"""D-034 synthetic GPU live-smoke runner with a closed, fail-closed boundary.

The public preparation path is pure validation: it does not import a provider
client, inspect a GPU, open a socket, or start a process.  The explicitly named
``execute_gpu_live_smoke`` path is the only live entrypoint and is gated by a
content-bound owner authority.  It never calls an agent ``predict`` method,
replay code, a backend, or a MobileWorld action executor.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import hashlib
import http.client
import importlib
import importlib.metadata
import json
import os
import re
import select
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import BinaryIO, NoReturn, cast

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    EvidenceRef,
    ExecutionMode,
    FailurePolicy,
    JsonValue,
    OperationKind,
    PlanOperation,
    PlanSetProfile,
    SpanRole,
    TransformationPlan,
    canonical_json_bytes,
    canonical_sha256,
    stable_id,
)
from mobile_world.offline.causal_replay.core import (
    restore_original,
    validate_plan_set,
    validate_pre_send,
)
from mobile_world.offline.causal_replay.registry import HistoryCodecRegistry
from mobile_world.offline.causal_replay_runner.live_preparation import (
    LIVE_PREPARATION_RECEIPT_SHA256,
    MODEL_CONFIG_MANIFEST_SHA256,
    LivePreparationReceipt,
    OpenAIChatCallDescriptor,
    VllmLaunchPlan,
    load_live_preparation,
    prepare_openai_chat_call,
    prepare_vllm_launch_plan,
)
from mobile_world.offline.g1_history_codecs.codecs import (
    CuratedSpanBinding,
    MaiRawReplayHistoryCodec,
    QwenFlatProgressHistoryCodec,
)

AUTHORITY_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-authority/v4"
SMOKE_PACKET_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-packet/v1"
PREPARATION_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-preparation/v2"
EXECUTION_RECEIPT_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-execution/v2"
DECISION_ID = "D-034"
AUTHORIZED_SCOPE = "SYNTHETIC_NON_CASE_GPU_LIVE_SMOKE_22_CALLS"
AUTHORIZED_GPU_UUID = "GPU-991ac45f-e9e9-1c25-590c-fb49ca752965"
MINIMUM_FREE_MEMORY_BYTES = 68_719_476_736
LAUNCH_SHIM_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-launch-shim/v3"
LAUNCH_SHIM_PATH = (
    "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/launch-shim.v3"
)
LAUNCH_SHIM_AUTHORITY_PATH = (
    "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/authority.v4.json"
)
LAUNCH_SHIM_TOKEN_PREFIX = "D034_STAGE0_V3"
EVIDENCE_ROOT_V4 = (
    "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/evidence-v4"
)
RUNTIME_SCRATCH_ROOT_V4 = (
    "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/d034-9845577c/runtime-scratch-v4"
)
TOOL_SHELL_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-tool-shell/v1"
TOOL_SHELL_PATH = "/bin/sh"
TOOL_SHELL_RESOLVED_PATH = "/usr/bin/dash"
TOOL_SHELL_SHA256 = "4f291296e89b784cd35479fca606f228126e3641f5bcaee68dee36583d7c9483"
TOOL_SHELL_BYTE_COUNT = 125_688
TOOL_SHELL_PT_INTERP_PATH = "/lib64/ld-linux-x86-64.so.2"
TOOL_SHELL_PT_INTERP_RESOLVED_PATH = "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
TOOL_SHELL_PT_INTERP_SHA256 = "9eb34cb2da3ae2a9398cc09b3cd2d069563ec40d9858cb711af15cd23fa80abf"
TOOL_SHELL_PT_INTERP_BYTE_COUNT = 240_936
TOOL_SHELL_LIBC_PATH = "/lib/x86_64-linux-gnu/libc.so.6"
TOOL_SHELL_LIBC_RESOLVED_PATH = "/usr/lib/x86_64-linux-gnu/libc.so.6"
TOOL_SHELL_LIBC_SHA256 = "c53819710b163d3f1d2541778590d58d3ef31cb0ed75adcbe059faac68c1e72d"
TOOL_SHELL_LIBC_BYTE_COUNT = 2_220_400
TOOL_SHELL_LD_SO_CACHE_PATH = "/etc/ld.so.cache"
TOOL_SHELL_LD_SO_CACHE_SHA256 = "a749eaf573738b83f29c68ef7837102e826eed2d304653c08e40cfd221490b78"
TOOL_SHELL_LD_SO_CACHE_BYTE_COUNT = 56_624
TOOL_SHELL_LD_SO_PRELOAD_PATH = "/etc/ld.so.preload"
TOOL_SHELL_AMBIENT_LD_LIBRARY_PATH = "/usr/local/cuda-13.0/lib64"
_TOOL_SHELL_PATH_COMPONENT_PATHS = (
    "/",
    "/bin",
    "/usr",
    "/usr/bin",
    "/bin/sh",
)
_TOOL_SHELL_INTERPRETER_COMPONENT_PATHS = (
    "/",
    "/lib64",
    "/usr",
    "/usr/lib64",
    TOOL_SHELL_PT_INTERP_PATH,
    "/lib",
    "/usr/lib",
    "/lib/x86_64-linux-gnu",
    "/usr/lib/x86_64-linux-gnu",
)
_TOOL_SHELL_AMBIENT_COMPONENT_PATHS = (
    "/",
    "/usr",
    "/usr/local",
    "/usr/local/cuda-13.0",
    TOOL_SHELL_AMBIENT_LD_LIBRARY_PATH,
    "/usr/local/cuda-13.0/targets",
    "/usr/local/cuda-13.0/targets/x86_64-linux",
    "/usr/local/cuda-13.0/targets/x86_64-linux/lib",
)
_TOOL_SHELL_SYSTEM_CONFIG_COMPONENT_PATHS = ("/", "/etc")
_TOOL_SHELL_FORBIDDEN_ENVIRONMENT_NAMES = (
    "BASH_ENV",
    "ENV",
    "GCONV_PATH",
    "GLIBC_TUNABLES",
    "IFS",
    "SHELLOPTS",
)
_TOOL_SHELL_FORBIDDEN_ENVIRONMENT_PREFIXES = ("BASH_FUNC_", "LD_")
_TOOL_SHELL_COMPONENT_EXPECTED: dict[str, tuple[str, str | None, str]] = {
    "/": ("directory", None, "/"),
    "/bin": ("symlink", "usr/bin", "/usr/bin"),
    "/bin/sh": ("symlink", "dash", TOOL_SHELL_RESOLVED_PATH),
    "/etc": ("directory", None, "/etc"),
    "/lib": ("symlink", "usr/lib", "/usr/lib"),
    "/lib/x86_64-linux-gnu": (
        "directory",
        None,
        "/usr/lib/x86_64-linux-gnu",
    ),
    "/lib64": ("symlink", "usr/lib64", "/usr/lib64"),
    TOOL_SHELL_PT_INTERP_PATH: (
        "symlink",
        "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
        TOOL_SHELL_PT_INTERP_RESOLVED_PATH,
    ),
    "/usr": ("directory", None, "/usr"),
    "/usr/bin": ("directory", None, "/usr/bin"),
    "/usr/lib": ("directory", None, "/usr/lib"),
    "/usr/lib64": ("directory", None, "/usr/lib64"),
    "/usr/lib/x86_64-linux-gnu": (
        "directory",
        None,
        "/usr/lib/x86_64-linux-gnu",
    ),
    "/usr/local": ("directory", None, "/usr/local"),
    "/usr/local/cuda-13.0": ("directory", None, "/usr/local/cuda-13.0"),
    TOOL_SHELL_AMBIENT_LD_LIBRARY_PATH: (
        "symlink",
        "targets/x86_64-linux/lib",
        "/usr/local/cuda-13.0/targets/x86_64-linux/lib",
    ),
    "/usr/local/cuda-13.0/targets": (
        "directory",
        None,
        "/usr/local/cuda-13.0/targets",
    ),
    "/usr/local/cuda-13.0/targets/x86_64-linux": (
        "directory",
        None,
        "/usr/local/cuda-13.0/targets/x86_64-linux",
    ),
    "/usr/local/cuda-13.0/targets/x86_64-linux/lib": (
        "directory",
        None,
        "/usr/local/cuda-13.0/targets/x86_64-linux/lib",
    ),
}
LAUNCH_SHIM_GCC_PATH = "/usr/bin/gcc"
LAUNCH_SHIM_BUILD_FLAGS = (
    "-std=c11",
    "-O2",
    "-nostdlib",
    "-static",
    "-fno-pie",
    "-no-pie",
    "-fno-stack-protector",
    "-fno-builtin",
    "-fno-asynchronous-unwind-tables",
    "-fno-unwind-tables",
    "-fno-ident",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wl,-e,_start,--build-id=none,-z,noexecstack,-z,norelro",
)
MODEL_ORDER = ("qwen3vl_8b", "mai_ui_8b")
REPLAY_SEEDS = (1729, 2718, 31415)
G1_5_ARMS = (
    "ORIGINAL",
    "MASK",
    "MASK_CORRECTION",
    "ORACLE_CLEAN",
    "SHAM_BENIGN_EDIT",
)
MODEL_CODECS = {
    "qwen3vl_8b": "mobileworld.g1.history-codec.qwen-flat-progress",
    "mai_ui_8b": "mobileworld.g1.history-codec.mai-raw-replay",
}
MODEL_IDENTITIES = {
    "qwen3vl_8b": {
        "repository": "Qwen/Qwen3-VL-8B-Instruct",
        "revision": "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b",
        "served_name": "Qwen3-VL-8B-Instruct",
    },
    "mai_ui_8b": {
        "repository": "Tongyi-MAI/MAI-UI-8B",
        "revision": "e00a0097abb9cc621cac5172d8c4809f0839c94e",
        "served_name": "MAI-UI-8B",
    },
}
G1_5_CPU_PUBLICATION_SHA256 = "cffd7f24bf09f2e18c012b2a96591064e8ba200378c7e9c920d6fdd8f068d018"
EXACT_ENDPOINT = {
    "origin": "http://127.0.0.1:18007",
    "path": "/v1/chat/completions",
    "health_path": "/health",
    "host": "127.0.0.1",
    "port": 18007,
}

_PIDFD_OPEN_SYSCALL_X86_64 = 434
_PIDFD_SEND_SIGNAL_SYSCALL_X86_64 = 424
_FD_CLOSE_UPPER_BOUND_EXCLUSIVE = 1_048_576
_SUPPLEMENTARY_GROUPS_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-supplementary-groups/v1"
_SUPPLEMENTARY_GROUPS_RUNTIME_SCHEMA_VERSION = (
    "mobileworld.g1.gpu-live-smoke-supplementary-groups-runtime/v1"
)
_SUPPLEMENTARY_GROUP_POLICY = "OWNER_APPROVED_RETAIN_EXACT_GROUPS_ZERO_CAPS_V1"
_HOST_GROUP_VECTOR = [1035, 109, 999]
_HOST_SUPPLEMENTARY_GIDS = [109, 999]
_INSIDE_SUPPLEMENTARY_GIDS_SORTED = [0, 65_534, 65_534]
_ISOLATED_PYTHON_FLAGS = (
    "-I",
    "-S",
    "-B",
    "-X",
    "pycache_prefix=/dev/null",
)
_OWNED_COMMAND_GATE_CODE = (
    "import base64,json,os,sys;"
    "fd=int(sys.argv[1]);"
    "env=json.loads(base64.urlsafe_b64decode(sys.argv[2]).decode('utf-8'));"
    "argv=sys.argv[3:];"
    "token=os.read(fd,1);os.close(fd);"
    "os.execve(argv[0],argv,env) if token==b'G' else os._exit(125)"
)
_OWNED_COMMAND_RECEIPT_SCHEMA_VERSION = "mobileworld.g1.gpu-live-smoke-owned-command/v1"

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_AUTHORITY_KEYS = {
    "schema_version",
    "authority_id",
    "decision_id",
    "authorized_scope",
    "authorized",
    "issued_at_utc",
    "expires_at_utc",
    "owner_uid",
    "gpu",
    "endpoint",
    "model_order",
    "models",
    "outer_runtime",
    "private_runtime",
    "client_runtime",
    "server_runtime",
    "bindings",
    "matrix",
    "policies",
    "source",
    "launch_shim",
    "tool_shell",
    "network_namespace",
    "evidence_root",
    "runtime_scratch_root",
}
_TOOL_SHELL_KEYS = {
    "schema_version",
    "path_binding",
    "resolved_binary",
    "elf",
    "interpreter_path_binding",
    "interpreter_binary",
    "interpreter_elf",
    "dependencies",
    "ld_so_cache",
    "ld_so_preload",
    "loader_resolution",
    "ambient_environment",
    "invocation",
    "formal_claims",
}
_TOOL_PATH_BINDING_KEYS = {"lexical_path", "component_bindings"}
_TOOL_PATH_COMPONENT_KEYS = {
    "path",
    "type",
    "symlink_target",
    "resolved_path",
    "owner_uid",
    "owner_gid",
    "mode",
    "nlink",
}
_TOOL_BINARY_KEYS = {
    "path",
    "resolved_path",
    "sha256",
    "byte_count",
    "owner_uid",
    "owner_gid",
    "mode",
    "nlink",
}
_TOOL_SHELL_ELF_KEYS = {
    "machine",
    "type",
    "elf_osabi",
    "pt_interp",
    "dt_needed",
    "rpath_runpath_allowed",
    "bind_now",
    "pie",
    "nx_stack",
}
_TOOL_INTERPRETER_ELF_KEYS = {"machine", "type", "elf_osabi"}
_TOOL_DEPENDENCY_KEYS = {"soname", "binary", "elf_osabi", "dt_needed"}
_TOOL_LD_SO_PRELOAD_KEYS = {"path", "present"}
_TOOL_LOADER_RESOLUTION_KEYS = {
    "ambient_path_binding",
    "system_configuration_path_binding",
    "ambient_tree_entry_count",
    "ambient_tree_entry_census_sha256",
    "recursive_forbidden_soname_count",
    "recursive_forbidden_soname_census_sha256",
    "ld_so_cache_selected_libc_path",
    "selected_libc_unique",
}
_TOOL_AMBIENT_ENVIRONMENT_KEYS = {
    "required",
    "forbidden_names",
    "forbidden_prefixes",
    "other_ld_environment_variables_allowed",
}
_TOOL_INVOCATION_KEYS = {
    "shell_option",
    "login",
    "tty",
    "command_grammar",
    "command_prefix",
    "command_prefix_sha256",
    "command_prefix_byte_count",
    "command_authority_sha256_byte_count",
    "command_total_byte_count",
}
_TOOL_FORMAL_CLAIM_KEYS = {
    "direct_exec_formally_proven",
    "pre_gate_dynamic_loader_closure_formally_proven",
}
_LAUNCH_SHIM_KEYS = {
    "schema_version",
    "path",
    "resolved_path",
    "sha256",
    "byte_count",
    "owner_uid",
    "owner_gid",
    "mode",
    "nlink",
    "source_path",
    "source_sha256",
    "shell_option",
    "token_prefix",
    "runner_cli_path",
    "smoke_packet_path",
    "model_config_manifest_path",
    "bootstrap_sha256",
    "bootstrap_byte_count",
    "confirmation",
    "elf_machine",
    "elf_type",
    "static",
    "pt_interp_allowed",
    "pt_dynamic_allowed",
    "dt_needed_allowed",
    "rpath_runpath_allowed",
    "init_array_allowed",
    "fini_array_allowed",
    "tls_segment_allowed",
    "writable_executable_segment_allowed",
    "executable_stack",
}
_GPU_KEYS = {
    "physical_index",
    "uuid",
    "cuda_visible_devices",
    "shared",
    "exclusive",
    "minimum_free_memory_bytes",
    "foreign_process_signaling_allowed",
}
_RUNTIME_CLIENT_KEYS = {
    "python_path",
    "python_resolved_path",
    "python_sha256",
    "site_packages_path",
    "openai_version",
    "site_packages_tree_sha256",
    "site_packages_tree_entry_count",
    "site_packages_tree_byte_count",
    "site_packages_owner_uid",
    "site_packages_owner_gid",
    "site_packages_directory_mode",
    "site_packages_regular_mode",
    "site_packages_executable_mode",
    "site_packages_symlinks_allowed",
    "site_packages_hardlinks_allowed",
}
_RUNTIME_SERVER_KEYS = {
    "python_path",
    "python_resolved_path",
    "python_sha256",
    "site_packages_path",
    "openai_version",
    "vllm_version",
    "torch_version",
    "site_packages_tree_sha256",
    "site_packages_tree_entry_count",
    "site_packages_tree_byte_count",
    "site_packages_owner_uid",
    "site_packages_owner_gid",
    "site_packages_directory_mode",
    "site_packages_regular_mode",
    "site_packages_executable_mode",
    "site_packages_symlinks_allowed",
    "site_packages_hardlinks_allowed",
}
_OUTER_RUNTIME_KEYS = {
    "python_path",
    "python_resolved_path",
    "python_sha256",
    "python_byte_count",
    "python_version",
    "python_flags",
    "stdlib_root",
    "stdlib_tree_sha256",
    "stdlib_tree_entry_count",
    "stdlib_tree_byte_count",
    "required_owner_uid",
    "required_owner_gid",
    "directory_mode",
    "regular_mode",
    "executable_mode",
    "symlinks_allowed",
    "hardlinks_allowed",
}
_PRIVATE_RUNTIME_KEYS = {
    "root",
    "python_path",
    "python_resolved_path",
    "python_sha256",
    "python_byte_count",
    "python_version",
    "python_flags",
    "stdlib_root",
    "tree_sha256",
    "tree_entry_count",
    "tree_byte_count",
    "owner_uid",
    "owner_gid",
    "directory_mode",
    "regular_mode",
    "executable_mode",
    "symlinks_allowed",
    "hardlinks_allowed",
}
_BINDING_KEYS = {
    "smoke_packet_sha256",
    "model_config_manifest_sha256",
    "live_preparation_receipt_sha256",
    "g1_5_cpu_publication_sha256",
    "runner_module_sha256",
    "runner_cli_sha256",
    "source_git_commit",
}
_NETWORK_NAMESPACE_KEYS = {
    "required",
    "implementation",
    "host_owner_uid",
    "host_owner_gid",
    "inside_owner_uid",
    "inside_owner_gid",
    "inside_unmapped_system_uid",
    "inside_unmapped_system_gid",
    "uid_map_line",
    "gid_map_line",
    "supplementary_groups",
    "env_path",
    "env_sha256",
    "unshare_path",
    "unshare_sha256",
    "ip_path",
    "ip_sha256",
    "setpriv_path",
    "setpriv_sha256",
    "nvidia_smi_path",
    "nvidia_smi_sha256",
    "nvidia_smi_byte_count",
    "pre_namespace_environment",
    "launcher_environment",
    "expected_interfaces",
    "loopback_up_required",
    "default_route_allowed",
    "external_network_allowed",
    "python_pycache_prefix",
    "fd_close_upper_bound_exclusive",
    "outer_fd_closure_receipt_sha256",
}
_SUPPLEMENTARY_GROUP_KEYS = {
    "schema_version",
    "owner_approved",
    "policy",
    "host_group_vector",
    "host_primary_gid",
    "host_supplementary_gids",
    "host_os_getgroups_sorted",
    "inside_supplementary_gids_sorted",
    "inside_groups_empty_required",
    "setpriv_group_option",
    "setgroups_control_expected",
    "capability_sets_all_zero_required",
    "no_new_privs_required",
    "docker_group_gid",
    "kvm_group_gid",
    "docker_kvm_filesystem_access_allowed",
    "docker_kvm_socket_access_allowed",
    "docker_kvm_action_allowed",
    "docker_af_unix_capability_retained",
    "kvm_device_capability_retained",
    "docker_kvm_invocation_allowed",
    "docker_kvm_use_mechanically_proven_absent",
    "formal_supplementary_group_isolation_proven",
    "nonformal_residual_disclosed",
}
_SUPPLEMENTARY_GROUP_RUNTIME_KEYS = {
    "schema_version",
    "phase",
    "policy",
    "owner_approved",
    "host_group_vector",
    "expected_inside_supplementary_gids_sorted",
    "observed_inside_supplementary_gids_sorted",
    "proc_status_groups_sorted",
    "inside_groups_empty_required",
    "setpriv_group_option",
    "setgroups_control",
    "capability_drop_required_at_phase",
    "capability_sets",
    "capability_sets_all_zero",
    "no_new_privs",
    "docker_kvm_filesystem_fd_count",
    "docker_kvm_unix_socket_fd_count",
    "docker_kvm_action_count",
    "foreign_process_operation_count",
    "docker_af_unix_capability_retained",
    "kvm_device_capability_retained",
    "docker_kvm_invocation_allowed",
    "docker_kvm_use_mechanically_proven_absent",
    "formal_supplementary_group_isolation_proven",
    "nonformal_residual_disclosed",
}
_LAUNCHER_ENVIRONMENT_KEYS = {
    "PATH",
    "CUDA_VISIBLE_DEVICES",
    "LD_LIBRARY_PATH",
    "HOME",
    "HF_HOME",
    "XDG_CACHE_HOME",
    "TORCH_HOME",
    "TRITON_CACHE_DIR",
    "VLLM_CACHE_ROOT",
    "TMPDIR",
    "PYTHONNOUSERSITE",
    "LC_ALL",
    "LANG",
    "GPU_SMOKE_OUTER_FD_CLOSURE_SHA256",
}
_PRE_NAMESPACE_ENVIRONMENT_KEYS = {"LC_CTYPE"}
_SERVER_SCRATCH_DIRECTORY_NAMES = {
    "HOME": "home",
    "HF_HOME": "hf-home",
    "XDG_CACHE_HOME": "xdg-cache",
    "TORCH_HOME": "torch-home",
    "TRITON_CACHE_DIR": "triton-cache",
    "VLLM_CACHE_ROOT": "vllm-cache",
    "TMPDIR": "tmp",
}
_SOURCE_AUTH_KEYS = {
    "worktree_root",
    "source_root",
    "git_path",
    "git_sha256",
    "head_commit",
    "critical_files",
    "bootstrap_manifest_sha256",
    "source_tree_sha256",
    "source_tree_entry_count",
    "source_tree_byte_count",
    "outer_bootstrap_code_sha256",
    "outer_bootstrap_code_byte_count",
}
_SOURCE_FILE_BINDING_KEYS = {"relative_path", "sha256"}
_CRITICAL_SOURCE_FILES = {
    "mobile_world_init": "MobileWorld/src/mobile_world/__init__.py",
    "gpu_live_smoke": "MobileWorld/src/mobile_world/offline/gpu_live_smoke.py",
    "causal_replay_contracts": ("MobileWorld/src/mobile_world/offline/causal_replay/contracts.py"),
    "causal_replay_core": "MobileWorld/src/mobile_world/offline/causal_replay/core.py",
    "causal_replay_registry": ("MobileWorld/src/mobile_world/offline/causal_replay/registry.py"),
    "live_preparation": (
        "MobileWorld/src/mobile_world/offline/causal_replay_runner/live_preparation.py"
    ),
    "history_codecs": ("MobileWorld/src/mobile_world/offline/g1_history_codecs/codecs.py"),
    "runner_cli": "MobileWorld/scripts/run_g1_gpu_live_smoke.py",
    "launch_shim_source": "MobileWorld/scripts/g1_gpu_live_smoke_launch_shim.c",
}
_CRITICAL_MODULE_NAMES = {
    "mobile_world_init": "mobile_world",
    "gpu_live_smoke": "mobile_world.offline.gpu_live_smoke",
    "causal_replay_contracts": "mobile_world.offline.causal_replay.contracts",
    "causal_replay_core": "mobile_world.offline.causal_replay.core",
    "causal_replay_registry": "mobile_world.offline.causal_replay.registry",
    "live_preparation": "mobile_world.offline.causal_replay_runner.live_preparation",
    "history_codecs": "mobile_world.offline.g1_history_codecs.codecs",
}
_MATRIX_KEYS = {
    "total_calls",
    "g1_4_calls",
    "g1_5_calls",
    "replay_seeds",
    "repeats_per_seed",
    "g1_5_seed",
    "arms",
}
_POLICY_KEYS = {
    "hf_hub_offline",
    "transformers_offline",
    "local_files_only",
    "loopback_only",
    "sdk_hidden_retries",
    "stream",
    "sequential_models",
    "model_co_residency_allowed",
    "generated_action_execution_allowed",
    "replay_allowed",
    "backend_restore_allowed",
    "mobileworld_action_allowed",
    "broad_process_signaling_allowed",
}
_MODEL_AUTH_KEYS = {
    "snapshot_path",
    "snapshot_tree_sha256",
    "snapshot_tree_entry_count",
    "snapshot_tree_byte_count",
    "repository",
    "revision",
    "served_name",
}
_PACKET_KEYS = {
    "schema_version",
    "packet_id",
    "synthetic_non_case",
    "secret_free",
    "formal_capsule",
    "contains_real_task_data",
    "generated_action_execution_allowed",
    "source_bindings",
    "calls",
}
_SOURCE_BINDING_KEYS = {
    "g1_5_cpu_publication_sha256",
    "compiler_contract",
    "fixtures",
}
_FIXTURE_BINDING_KEYS = {
    "relative_path",
    "file_sha256",
    "fixture_id",
    "fixture_request_sha256",
}
_CALL_KEYS = {
    "call_id",
    "phase",
    "model_id",
    "codec_id",
    "seed",
    "repeat_index",
    "arm",
    "application_request",
    "diff",
    "mapping",
    "render_evidence",
}
_RENDER_EVIDENCE_KEYS = {
    "source_application_request_sha256",
    "rendered_application_request_sha256",
    "diff_sha256",
    "mapping_sha256",
    "target_only_diff",
    "source_mapping_reversible",
    "provider_invocation_allowed",
}
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "authorization_header",
    "cookie",
    "cookies",
    "password",
    "secret",
    "token",
}
_CREDENTIAL_BYTE_PATTERNS = (
    ("bearer", re.compile(rb"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{8,}")),
    ("openai_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{10,}")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    (
        "credential_assignment",
        re.compile(
            rb"(?i)\b(?:api[_-]?key|authorization|password|secret|access[_-]?token)"
            rb"[ \t]*[:=][ \t]*[^\s,;]{4,}"
        ),
    ),
)
_PARSER_FILES = {
    "qwen3vl_8b": (
        "mobile_world.agents.implementations.qwen3vl",
        "MobileWorld/src/mobile_world/agents/implementations/qwen3vl.py",
        "202a04443eaa1d2f4c776b73bc315e65617d28df882ee2fac305849a7f79ac82",
    ),
    "mai_ui_8b": (
        "mobile_world.agents.implementations.mai_ui_agent",
        "MobileWorld/src/mobile_world/agents/implementations/mai_ui_agent.py",
        "0c18f8a5362d8e93fc9798882d12945d2aef152e38fbe9645470bfb6c74f549f",
    ),
}
_FIXTURES = {
    "qwen3vl_8b": {
        "relative_path": "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json",
        "file_sha256": "60f19821f782cd20ded8a926ea4466dff151cc5163a4728f9d0c761ae08b34be",
        "fixture_id": "g15-qwen-flat-progress-captured-redacted-v1",
        "fixture_request_sha256": "72f1396204e56c05b49a2a8564650f915c780d9bfa32f455f1cef3320abd6a33",
    },
    "mai_ui_8b": {
        "relative_path": "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/mai_raw_replay.captured.v1.json",
        "file_sha256": "b9e025b0b4990e9e3fe259b7dd21e2919de6538c00b42f2936c7b9fe403e9b40",
        "fixture_id": "g15-mai-raw-replay-captured-redacted-v1",
        "fixture_request_sha256": "c2ee086c6e5e659c4f904fbb74afefc9c89f7aabcfd47441ce65cce332d37a7d",
    },
}
PACKET_COMPILER_CONTRACT = "mobileworld.g1.gpu-live-smoke-packet-compiler/v1"


class GpuLiveSmokeError(RuntimeError):
    """Stable fail-closed error raised by the D-034 boundary."""

    def __init__(self, code: str, message: str, *, json_path: str = "$") -> None:
        super().__init__(f"{code}: {message} at {json_path}")
        self.code = code
        self.json_path = json_path
        self.terminal_receipt: dict[str, JsonValue] | None = None
        self.application_visible_attempt_count: int | None = None
        self.physical_request_count: int | None = None
        self.physical_request_count_upper_bound: int | None = None
        self.execution_detail: JsonValue = None
        self.received_raw_response: bytes | None = None
        self.received_response_metadata: dict[str, JsonValue] | None = None
        self.failed_launch_cleanup_handle: object | None = None


def _fail(code: str, message: str, path: str = "$") -> NoReturn:
    raise GpuLiveSmokeError(code, message, json_path=path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stat_identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA_RE.fullmatch(cast(str, value)) is not None


def _closed(value: object, keys: set[str], path: str) -> dict[str, JsonValue]:
    if type(value) is not dict or set(value) != keys:
        _fail("GPU_SMOKE_CLOSED_SHAPE_INVALID", "object keys differ from the closed contract", path)
    return cast(dict[str, JsonValue], value)


def _canonical_object(value: object, path: str) -> tuple[dict[str, JsonValue], bytes]:
    if type(value) is not dict:
        _fail("GPU_SMOKE_JSON_ROOT_INVALID", "JSON root must be an object", path)
    try:
        data = canonical_json_bytes(cast(JsonValue, value))
    except Exception as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_JSON_CANONICALIZATION_FAILED",
            "JSON value is not canonicalizable",
            json_path=path,
        ) from exc
    return cast(dict[str, JsonValue], value), data


def _duplicate_rejecting_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_nofollow(
    path: str | os.PathLike[str], *, maximum_bytes: int
) -> tuple[dict[str, JsonValue], bytes]:
    raw_path = os.fspath(path)
    if type(raw_path) is not str or not raw_path.startswith("/") or "\x00" in raw_path:
        _fail("GPU_SMOKE_PATH_INVALID", "input path must be lexical absolute")
    pure = PurePosixPath(raw_path)
    if str(pure) != raw_path or any(part in {".", ".."} for part in pure.parts):
        _fail("GPU_SMOKE_PATH_INVALID", "input path must be normalized")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        _fail("GPU_SMOKE_NOFOLLOW_UNAVAILABLE", "O_NOFOLLOW is required")
    try:
        fd = os.open(raw_path, flags | os.O_NOFOLLOW)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_INPUT_UNREADABLE", "input could not be opened safely"
        ) from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            _fail("GPU_SMOKE_INPUT_UNSAFE", "input is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        _fail("GPU_SMOKE_INPUT_CHANGED", "input changed while being read")
    data = b"".join(chunks)
    if len(data) != before.st_size:
        _fail("GPU_SMOKE_INPUT_CHANGED", "input read was incomplete")
    try:
        value = json.loads(data, object_pairs_hook=_duplicate_rejecting_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GpuLiveSmokeError("GPU_SMOKE_JSON_INVALID", "input is not strict JSON") from exc
    root, canonical = _canonical_object(value, "$")
    if data != canonical:
        _fail("GPU_SMOKE_JSON_NOT_CANONICAL", "input bytes must be exact canonical JSON")
    return root, data


def _absolute_lexical(value: object, path: str) -> str:
    if type(value) is not str or not cast(str, value).startswith("/"):
        _fail("GPU_SMOKE_PATH_INVALID", "path must be lexical absolute", path)
    text = cast(str, value)
    pure = PurePosixPath(text)
    if text != str(pure) or any(part in {".", ".."} for part in pure.parts):
        _fail("GPU_SMOKE_PATH_INVALID", "path must be normalized", path)
    return text


def _open_absolute_nofollow_file(path: str, flags: int) -> int:
    """Open an absolute file through pinned, non-symlink parent directories."""

    lexical = _absolute_lexical(path, "$.path")
    parts = lexical.split("/")[1:]
    if not parts:
        _fail("GPU_SMOKE_PATH_INVALID", "path must name a file", "$.path")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open("/", directory_flags)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _parse_utc(value: object, path: str) -> datetime:
    if type(value) is not str or not cast(str, value).endswith("Z"):
        _fail("GPU_SMOKE_AUTHORITY_TIME_INVALID", "timestamp must be UTC with Z", path)
    try:
        parsed = datetime.fromisoformat(cast(str, value)[:-1] + "+00:00")
    except ValueError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_AUTHORITY_TIME_INVALID", "timestamp is not RFC3339", json_path=path
        ) from exc
    if parsed.tzinfo != UTC:
        _fail("GPU_SMOKE_AUTHORITY_TIME_INVALID", "timestamp must be UTC", path)
    return parsed


def _scan_for_secrets(value: JsonValue, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _SECRET_KEYS:
                _fail(
                    "GPU_SMOKE_SECRET_FIELD_FORBIDDEN",
                    "secret-bearing key is forbidden",
                    f"{path}.{key}",
                )
            _scan_for_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_secrets(child, f"{path}[{index}]")


def _credential_scan_receipt(data: bytes, *, artifact_kind: str) -> dict[str, JsonValue]:
    matched = [name for name, pattern in _CREDENTIAL_BYTE_PATTERNS if pattern.search(data)]
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-credential-scan/v1",
        "artifact_kind": artifact_kind,
        "artifact_sha256": _sha256(data),
        "artifact_byte_count": len(data),
        "pattern_set": [name for name, _pattern in _CREDENTIAL_BYTE_PATTERNS],
        "match_count": len(matched),
        "secret_material_persisted": False,
    }


def _scan_bytes_for_credentials(data: bytes, *, artifact_kind: str) -> dict[str, JsonValue]:
    receipt = _credential_scan_receipt(data, artifact_kind=artifact_kind)
    if receipt["match_count"] != 0:
        error = GpuLiveSmokeError(
            "GPU_SMOKE_SECRET_FIELD_FORBIDDEN",
            f"{artifact_kind} matched a forbidden credential pattern",
        )
        error.execution_detail = {"credential_scan": receipt}
        raise error
    return receipt


@dataclass(frozen=True, slots=True)
class GpuLiveAuthority:
    value: dict[str, JsonValue]
    canonical_bytes: bytes
    sha256: str

    @property
    def authority_id(self) -> str:
        return cast(str, self.value["authority_id"])

    @property
    def owner_uid(self) -> int:
        return cast(int, self.value["owner_uid"])

    @property
    def gpu(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.value["gpu"])

    @property
    def models(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.value["models"])

    @property
    def bindings(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.value["bindings"])

    @property
    def matrix(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.value["matrix"])

    @property
    def launch_shim(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.value["launch_shim"])

    @property
    def tool_shell(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.value["tool_shell"])

    @property
    def network_namespace(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.value["network_namespace"])

    @property
    def source(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.value["source"])

    @property
    def evidence_root(self) -> str:
        return cast(str, self.value["evidence_root"])

    @property
    def runtime_scratch_root(self) -> str:
        return cast(str, self.value["runtime_scratch_root"])


@dataclass(frozen=True, slots=True)
class GpuSmokeCall:
    value: dict[str, JsonValue]
    application_request_bytes: bytes

    @property
    def model_id(self) -> str:
        return cast(str, self.value["model_id"])

    @property
    def phase(self) -> str:
        return cast(str, self.value["phase"])

    @property
    def seed(self) -> int:
        return cast(int, self.value["seed"])


@dataclass(frozen=True, slots=True)
class GpuSmokePacket:
    value: dict[str, JsonValue]
    canonical_bytes: bytes
    sha256: str
    calls: tuple[GpuSmokeCall, ...]


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Exact process identity used for every signal decision."""

    uid: int
    pid: int
    ppid: int
    pgid: int
    sid: int
    starttime_ticks: int
    executable_path: str
    executable_sha256: str
    argv: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "uid": self.uid,
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "sid": self.sid,
            "starttime_ticks": self.starttime_ticks,
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
            "argv": list(self.argv),
            "argv_sha256": _sha256(canonical_json_bytes(list(self.argv))),
        }


@dataclass(frozen=True, slots=True)
class _OwnedCommandResult:
    """Bounded output and an auditable receipt for one auxiliary command."""

    returncode: int
    stdout: bytes
    stderr: bytes
    receipt: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _PinnedOwnedCommandMember:
    """One command-tree member pinned for the full observation lifetime."""

    identity: ProcessIdentity
    pidfd: int
    procdir_fd: int
    depth: int
    allowed_exec_argv: tuple[str, ...] | None = None
    allowed_exec_path: str | None = None
    allowed_exec_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _MinimalDirectChildIdentity:
    uid: int
    pid: int
    ppid: int
    pgid: int
    sid: int
    starttime_ticks: int


@dataclass(frozen=True, slots=True)
class _FailedLaunchCleanupHandle:
    process: subprocess.Popen[bytes]
    acquisition_pidfd: int
    root_minimal: _MinimalDirectChildIdentity | None
    recorded_tree: tuple[ProcessIdentity, ...]
    model_id: str
    expected_argv: tuple[str, ...]
    gpu_uuid: str
    host: str
    port: int
    environment_sha256: str
    evidence_frozen: bool


@dataclass(frozen=True, slots=True)
class _RuntimeScratch:
    run_id: str
    run_root: str
    model_directories: dict[str, str]
    pre_censuses: dict[str, dict[str, JsonValue]]


@dataclass(frozen=True, slots=True)
class ProcessOwnershipGuard:
    """Root service identity plus immutable model/port expectations."""

    root: ProcessIdentity
    model_id: str
    snapshot_path: str
    served_name: str
    host: str
    port: int
    expected_argv: tuple[str, ...]
    environment_sha256: str
    service_tree: tuple[ProcessIdentity, ...] = ()
    gpu_uuid: str = AUTHORIZED_GPU_UUID

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "root": self.root.to_dict(),
            "model_id": self.model_id,
            "snapshot_path": self.snapshot_path,
            "served_name": self.served_name,
            "host": self.host,
            "port": self.port,
            "gpu_uuid": self.gpu_uuid,
            "expected_argv": list(self.expected_argv),
            "expected_argv_sha256": _sha256(canonical_json_bytes(list(self.expected_argv))),
            "environment_sha256": self.environment_sha256,
            "service_tree": [item.to_dict() for item in self.service_tree],
            "broad_process_signaling_allowed": False,
        }


def _open_secure_directory(path: Path, *, create: bool) -> int:
    """Open an absolute directory through no-follow dirfds, optionally creating it."""

    raw = _absolute_lexical(str(path), "$.evidence_path")
    if raw == "/":
        _fail("GPU_SMOKE_EVIDENCE_ROOT_INVALID", "filesystem root is not an evidence directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        _fail("GPU_SMOKE_NOFOLLOW_UNAVAILABLE", "O_NOFOLLOW is required")
    flags |= os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for part in PurePosixPath(raw).parts[1:]:
            try:
                following = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                following = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_EVIDENCE_ROOT_INVALID",
                    "evidence path contains a symlink or non-directory component",
                ) from exc
            os.close(current)
            current = following
        return current
    except Exception:
        os.close(current)
        raise


def _assert_private_directory_fd(fd: int, *, label: str) -> os.stat_result:
    metadata = os.fstat(fd)
    if not (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    ):
        _fail(
            "GPU_SMOKE_EVIDENCE_ROOT_INVALID",
            f"{label} must be an owner-only UID-owned directory (0700)",
        )
    return metadata


def _assert_private_regular_file(metadata: os.stat_result, *, label: str) -> None:
    if not (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == 1
    ):
        _fail(
            "GPU_SMOKE_EVIDENCE_ROOT_INVALID",
            f"{label} must be a singly linked UID-owned regular file with mode 0600",
        )


def _secure_create_directory(path: Path, *, exclusive: bool) -> None:
    parent_fd = _open_secure_directory(path.parent, create=True)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            if exclusive:
                _fail(
                    "GPU_SMOKE_EVIDENCE_RUN_EXISTS",
                    "write-once evidence run directory already exists",
                )
        child_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        child_fd = os.open(path.name, child_flags, dir_fd=parent_fd)
        try:
            _assert_private_directory_fd(child_fd, label="evidence directory")
        finally:
            os.close(child_fd)
    finally:
        os.close(parent_fd)


def _read_regular_fd(fd: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(fd, min(1_048_576, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != expected_size:
        _fail("GPU_SMOKE_EVIDENCE_COLLISION", "existing evidence size changed")
    return data


class _EvidenceStore:
    """Write-once content objects, events, and a discoverable terminal seal."""

    def __init__(self, root: str, run_id: str) -> None:
        root_path = Path(_absolute_lexical(root, "$.evidence_root"))
        repository_root = Path(__file__).resolve().parents[4]
        if (
            root_path == Path("/")
            or root_path == repository_root
            or repository_root in root_path.parents
        ):
            _fail(
                "GPU_SMOKE_EVIDENCE_ROOT_INVALID",
                "evidence root must be outside the Git worktree and not filesystem root",
                "$.evidence_root",
            )
        if _ID_RE.fullmatch(run_id) is None:
            _fail("GPU_SMOKE_EVIDENCE_RUN_ID_INVALID", "evidence run ID is not canonical")
        _secure_create_directory(root_path, exclusive=False)
        root_fd = _open_secure_directory(root_path, create=False)
        try:
            _assert_private_directory_fd(root_fd, label="evidence root")
        finally:
            os.close(root_fd)
        self.root = root_path
        self.run_id = run_id
        self.run_dir = root_path / "runs" / run_id
        self.object_root = root_path / "objects" / "sha256"
        _secure_create_directory(root_path / "runs", exclusive=False)
        _secure_create_directory(root_path / "objects", exclusive=False)
        _secure_create_directory(self.object_root, exclusive=False)
        _secure_create_directory(self.run_dir, exclusive=True)
        self._seq = 0
        self._sealed = False
        self._event_files: list[str] = []
        self._object_refs: dict[str, dict[str, JsonValue]] = {}
        self._owned_command_refs: dict[str, dict[str, JsonValue]] = {}

    def owned_command_references(self) -> list[dict[str, JsonValue]]:
        return [self._owned_command_refs[key] for key in sorted(self._owned_command_refs)]

    @staticmethod
    def _install(path: Path, data: bytes) -> None:
        if not path.is_absolute() or not path.name:
            _fail("GPU_SMOKE_EVIDENCE_ROOT_INVALID", "evidence object path is invalid")
        parent_fd = _open_secure_directory(path.parent, create=True)
        _assert_private_directory_fd(parent_fd, label="evidence parent directory")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            try:
                fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                read_fd = os.open(
                    path.name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                try:
                    before = os.fstat(read_fd)
                    _assert_private_regular_file(before, label="existing evidence file")
                    if before.st_size != len(data):
                        _fail(
                            "GPU_SMOKE_EVIDENCE_COLLISION",
                            "existing evidence is not the expected regular file",
                        )
                    existing = _read_regular_fd(read_fd, before.st_size)
                    after = os.fstat(read_fd)
                    _assert_private_regular_file(after, label="existing evidence file")
                    if _stat_identity(before) != _stat_identity(after) or existing != data:
                        _fail(
                            "GPU_SMOKE_EVIDENCE_COLLISION",
                            "existing write-once evidence differs",
                        )
                finally:
                    os.close(read_fd)
                return
            try:
                created = os.fstat(fd)
                _assert_private_regular_file(created, label="new evidence file")
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written <= 0:
                        _fail("GPU_SMOKE_EVIDENCE_WRITE_FAILED", "evidence write made no progress")
                    offset += written
                os.fsync(fd)
                finished = os.fstat(fd)
                _assert_private_regular_file(finished, label="new evidence file")
                if finished.st_ino != created.st_ino or finished.st_size != len(data):
                    _fail(
                        "GPU_SMOKE_EVIDENCE_WRITE_FAILED",
                        "new evidence identity changed while it was written",
                    )
            finally:
                os.close(fd)
            os.fsync(parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_EVIDENCE_ROOT_INVALID",
                    "evidence path contains a symlink",
                ) from exc
            raise
        finally:
            os.close(parent_fd)

    def object(self, data: bytes, media_type: str) -> dict[str, JsonValue]:
        if self._sealed:
            _fail("GPU_SMOKE_EVIDENCE_ALREADY_SEALED", "evidence run is already terminal")
        digest = _sha256(data)
        relative = PurePosixPath("objects") / "sha256" / digest[:2] / digest
        self._install(self.root / Path(relative), data)
        reference: dict[str, JsonValue] = {
            "relative_path": str(relative),
            "sha256": digest,
            "byte_count": len(data),
            "media_type": media_type,
        }
        self._object_refs[digest] = reference
        return reference

    def event(self, kind: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if self._sealed:
            _fail("GPU_SMOKE_EVIDENCE_ALREADY_SEALED", "evidence run is already terminal")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", kind) is None:
            _fail("GPU_SMOKE_EVIDENCE_EVENT_INVALID", "event kind is not stable")
        materialized_payload = _materialize_owned_command_receipts(self, payload)
        if type(materialized_payload) is not dict:
            _fail("GPU_SMOKE_EVIDENCE_EVENT_INVALID", "event payload is not an object")
        self._seq += 1
        value: dict[str, JsonValue] = {
            "schema_version": "mobileworld.g1.gpu-live-smoke-event/v1",
            "run_id": self.run_id,
            "seq": self._seq,
            "event_kind": kind,
            "payload": cast(dict[str, JsonValue], materialized_payload),
            "generated_action_executed": False,
            "replay_executed": False,
        }
        data = canonical_json_bytes(value)
        digest = _sha256(data)
        name = f"{self._seq:04d}-{digest}.json"
        self._install(self.run_dir / name, data)
        self._event_files.append(name)
        self.object(data, "application/json")
        return value

    def seal(
        self,
        status: str,
        terminal_payload: dict[str, JsonValue],
        *,
        validation_documents: dict[str, dict[str, JsonValue]] | None = None,
    ) -> dict[str, JsonValue]:
        if self._sealed:
            _fail("GPU_SMOKE_EVIDENCE_ALREADY_SEALED", "evidence run is already terminal")
        if status not in {"PASS", "FAIL"}:
            _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "terminal status is invalid")
        materialized_terminal = _materialize_owned_command_receipts(self, terminal_payload)
        if type(materialized_terminal) is not dict:
            _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "terminal payload is not an object")
        terminal_payload = cast(dict[str, JsonValue], materialized_terminal)
        if validation_documents is not None:
            validation = _preseal_schema_validation(
                self,
                status,
                terminal_payload,
                validation_documents,
            )
            validation_ref = self.object(canonical_json_bytes(validation), "application/json")
            terminal_payload = {**terminal_payload, "schema_validation": validation_ref}
        object_refs = [self._object_refs[key] for key in sorted(self._object_refs)]
        manifest_subject: dict[str, JsonValue] = {
            "schema_version": "mobileworld.g1.gpu-live-smoke-manifest-subject/v1",
            "run_id": self.run_id,
            "status": status,
            "exact_event_files": list(self._event_files),
            "event_count": len(self._event_files),
            "exact_content_objects": object_refs,
            "content_object_count": len(object_refs),
            "self_excluded_content_object_roles": [
                "MANIFEST_OBJECT",
                "TERMINAL_RECEIPT_OBJECT",
            ],
            "self_excluded_content_object_count": 2,
            "content_object_census_rule": (
                "EXACT_PRESEAL_OBJECTS_PLUS_TWO_EXPLICIT_SELF_EXCLUSIONS"
            ),
        }
        manifest_subject_sha256 = canonical_sha256(manifest_subject)
        manifest: dict[str, JsonValue] = {
            **manifest_subject,
            "manifest_subject_sha256": manifest_subject_sha256,
        }
        manifest_data = canonical_json_bytes(manifest)
        manifest_ref = self.object(manifest_data, "application/json")
        manifest_name = f"manifest-{cast(str, manifest_ref['sha256'])}.json"
        receipt_subject: dict[str, JsonValue] = {
            "schema_version": EXECUTION_RECEIPT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": status,
            "manifest": manifest_ref,
            **terminal_payload,
        }
        receipt_subject_sha256 = canonical_sha256(receipt_subject)
        receipt: dict[str, JsonValue] = {
            **receipt_subject,
            "receipt_subject_sha256": receipt_subject_sha256,
        }
        receipt_data = canonical_json_bytes(receipt)
        receipt_ref = self.object(receipt_data, "application/json")
        receipt_name = f"terminal-{cast(str, receipt_ref['sha256'])}.json"
        if validation_documents is not None:
            _validate_final_schema_objects(
                self,
                manifest_ref,
                receipt_ref,
                validation_documents,
            )
        try:
            self._install(self.run_dir / manifest_name, manifest_data)
            self._install(self.run_dir / receipt_name, receipt_data)
            _validate_published_run_closure(
                self,
                manifest_name=manifest_name,
                manifest_data=manifest_data,
                receipt_name=receipt_name,
                receipt_data=receipt_data,
            )
        except BaseException:
            _remove_unsealed_run_file(self, manifest_name)
            _remove_unsealed_run_file(self, receipt_name)
            raise
        self._sealed = True
        return {
            **receipt,
            "terminal_receipt": receipt_ref,
            "run_relative_path": f"runs/{self.run_id}",
            "terminal_file": receipt_name,
            "manifest_file": manifest_name,
        }


def _materialize_owned_command_receipts(
    store: _EvidenceStore,
    value: JsonValue,
) -> JsonValue:
    """Replace every embedded auxiliary-command receipt with its content ref."""

    if isinstance(value, list):
        return [_materialize_owned_command_receipts(store, item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("schema_version") == _OWNED_COMMAND_RECEIPT_SCHEMA_VERSION:
        _scan_for_secrets(cast(JsonValue, value))
        reference = store.object(canonical_json_bytes(value), "application/json")
        digest = cast(str, reference["sha256"])
        store._owned_command_refs[digest] = reference
        return reference
    return {key: _materialize_owned_command_receipts(store, child) for key, child in value.items()}


_FINAL_SCHEMA_FILES = {
    "authority": "gpu_smoke_authority.schema.json",
    "packet": "gpu_smoke_packet.schema.json",
    "preparation": "gpu_smoke_preparation.schema.json",
    "event": "gpu_smoke_event.schema.json",
    "call": "gpu_smoke_call.schema.json",
    "lifecycle": "gpu_smoke_lifecycle.schema.json",
    "owned_command": "gpu_smoke_owned_command.schema.json",
    "manifest": "gpu_smoke_manifest.schema.json",
    "execution": "gpu_smoke_execution.schema.json",
    "error": "gpu_smoke_error.schema.json",
}


def _remove_unsealed_run_file(store: _EvidenceStore, name: str) -> None:
    if (
        store._sealed
        or Path(name).name != name
        or not re.fullmatch(r"(?:manifest|terminal)-[0-9a-f]{64}\.json", name)
    ):
        return
    directory_fd = _open_secure_directory(store.run_dir, create=False)
    try:
        _assert_private_directory_fd(directory_fd, label="evidence run directory")
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _validate_published_run_closure(
    store: _EvidenceStore,
    *,
    manifest_name: str,
    manifest_data: bytes,
    receipt_name: str,
    receipt_data: bytes,
) -> None:
    directory_fd = _open_secure_directory(store.run_dir, create=False)
    try:
        _assert_private_directory_fd(directory_fd, label="evidence run directory")
        actual_names = sorted(os.listdir(directory_fd))
    finally:
        os.close(directory_fd)
    expected_names = sorted([*store._event_files, manifest_name, receipt_name])
    if actual_names != expected_names:
        _fail(
            "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
            "published run directory differs from the exact event/manifest/terminal census",
        )
    for name in expected_names:
        match = re.fullmatch(
            r"(?:[0-9]{4}-|manifest-|terminal-)([0-9a-f]{64})\.json",
            name,
        )
        if match is None or _sha256(_read_run_evidence_bytes(store, name)) != match.group(1):
            _fail(
                "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
                "published run evidence filename/content digest differs",
            )
    if not (
        _read_run_evidence_bytes(store, manifest_name) == manifest_data
        and _read_run_evidence_bytes(store, receipt_name) == receipt_data
    ):
        _fail(
            "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
            "published named roots differ from validated content objects",
        )


def _read_evidence_bytes(
    store: _EvidenceStore,
    reference: dict[str, JsonValue],
) -> bytes:
    relative = cast(str, reference.get("relative_path"))
    expected_sha = cast(str, reference.get("sha256"))
    expected_size = cast(int, reference.get("byte_count"))
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or str(pure) != relative
        or tuple(pure.parts[:2]) != ("objects", "sha256")
    ):
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "content reference path is unsafe")
    path = store.root / Path(pure)
    parent_fd = _open_secure_directory(path.parent, create=False)
    try:
        _assert_private_directory_fd(parent_fd, label="content-object parent directory")
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            before = os.fstat(fd)
            _assert_private_regular_file(before, label="content-object file")
            if before.st_size != expected_size:
                _fail(
                    "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
                    "content reference is not the expected regular file",
                )
            data = _read_regular_fd(fd, expected_size)
            after = os.fstat(fd)
            _assert_private_regular_file(after, label="content-object file")
        finally:
            os.close(fd)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
            "content reference cannot be read without following links",
        ) from exc
    finally:
        os.close(parent_fd)
    if _stat_identity(before) != _stat_identity(after) or _sha256(data) != expected_sha:
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "content reference changed")
    return data


def _read_run_evidence_bytes(store: _EvidenceStore, name: str) -> bytes:
    if Path(name).name != name:
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "run evidence name is unsafe")
    path = store.run_dir / name
    directory_fd = _open_secure_directory(path.parent, create=False)
    try:
        _assert_private_directory_fd(directory_fd, label="evidence run directory")
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            before = os.fstat(fd)
            _assert_private_regular_file(before, label="run evidence file")
            if before.st_size > 64 * 1024 * 1024:
                _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "run evidence file is unsafe")
            data = _read_regular_fd(fd, before.st_size)
            after = os.fstat(fd)
            _assert_private_regular_file(after, label="run evidence file")
        finally:
            os.close(fd)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
            "run evidence file cannot be read without following links",
        ) from exc
    finally:
        os.close(directory_fd)
    if _stat_identity(before) != _stat_identity(after):
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "run evidence file changed")
    return data


def _load_final_schemas() -> tuple[dict[str, object], list[dict[str, JsonValue]]]:
    schema_root = (
        Path(__file__).resolve().parents[4]
        / "mobileworld_audit_handoff"
        / "schemas"
        / "g1_gpu_smoke"
    )
    schemas: dict[str, object] = {}
    receipts: list[dict[str, JsonValue]] = []
    for name, filename in _FINAL_SCHEMA_FILES.items():
        path = schema_root / filename
        try:
            data = path.read_bytes()
            schema = json.loads(data, object_pairs_hook=_duplicate_rejecting_pairs)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
                "final evidence schema is unreadable or invalid JSON",
            ) from exc
        schemas[name] = schema
        receipts.append(
            {
                "name": name,
                "relative_path": str(path.relative_to(Path(__file__).resolve().parents[4])),
                "sha256": _sha256(data),
                "byte_count": len(data),
            }
        )
    return schemas, receipts


def _schema_validate(schema: object, instance: object, *, label: str) -> None:
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-not-found]

        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(instance)
    except Exception as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
            f"{label} failed frozen Draft 2020-12 validation ({type(exc).__name__})",
        ) from exc


def _preseal_schema_validation(
    store: _EvidenceStore,
    status: str,
    terminal_payload: dict[str, JsonValue],
    documents: dict[str, dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    if set(documents) != {"authority", "packet", "preparation"}:
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "validation documents are not closed")
    schemas, schema_receipts = _load_final_schemas()
    for name in ("authority", "packet", "preparation"):
        _schema_validate(schemas[name], documents[name], label=name)
    event_count = 0
    for name in store._event_files:
        event = json.loads(_read_run_evidence_bytes(store, name))
        _schema_validate(schemas["event"], event, label="event")
        event_count += 1
    call_refs = cast(list[dict[str, JsonValue]], terminal_payload["call_receipts"])
    for reference in call_refs:
        _schema_validate(
            schemas["call"],
            json.loads(_read_evidence_bytes(store, reference)),
            label="call",
        )
    lifecycle_refs = cast(list[dict[str, JsonValue]], terminal_payload["lifecycle_receipts"])
    for reference in lifecycle_refs:
        _schema_validate(
            schemas["lifecycle"],
            json.loads(_read_evidence_bytes(store, reference)),
            label="lifecycle",
        )
    owned_command_refs = store.owned_command_references()
    for reference in owned_command_refs:
        _schema_validate(
            schemas["owned_command"],
            json.loads(_read_evidence_bytes(store, reference)),
            label="owned command",
        )
    if status == "FAIL":
        _schema_validate(schemas["error"], terminal_payload["error_code"], label="error")
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-schema-validation/v1",
        "run_id": store.run_id,
        "status": status,
        "schemas": cast(JsonValue, schema_receipts),
        "schema_count": len(schema_receipts),
        "authority_valid": True,
        "packet_valid": True,
        "preparation_valid": True,
        "event_count_validated": event_count,
        "call_count_validated": len(call_refs),
        "lifecycle_count_validated": len(lifecycle_refs),
        "owned_command_receipt_count_validated": len(owned_command_refs),
        "owned_command_receipts_validated": cast(JsonValue, owned_command_refs),
        "manifest_validated_before_terminal_publish": True,
        "stored_execution_validated_before_terminal_publish": True,
        "actual_content_bytes_read": True,
        "manifest_self_exclusion_rule_validated": True,
        "run_directory_exact_census_required_after_publish": True,
        "validation_failure_is_terminal": True,
    }


def _validate_final_schema_objects(
    store: _EvidenceStore,
    manifest_ref: dict[str, JsonValue],
    receipt_ref: dict[str, JsonValue],
    documents: dict[str, dict[str, JsonValue]],
) -> None:
    del documents
    schemas, schema_receipts = _load_final_schemas()
    manifest_data = _read_evidence_bytes(store, manifest_ref)
    receipt_data = _read_evidence_bytes(store, receipt_ref)
    manifest = cast(dict[str, JsonValue], json.loads(manifest_data))
    receipt = cast(dict[str, JsonValue], json.loads(receipt_data))
    raw_validation_ref = receipt.get("schema_validation")
    if type(raw_validation_ref) is not dict:
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "schema validation reference is absent")
    validation_ref = cast(dict[str, JsonValue], raw_validation_ref)
    validation = cast(
        dict[str, JsonValue],
        json.loads(_read_evidence_bytes(store, validation_ref)),
    )
    if validation.get("schemas") != schema_receipts:
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "schema hashes changed during seal")
    owned_command_refs = store.owned_command_references()
    if not (
        validation.get("owned_command_receipt_count_validated") == len(owned_command_refs)
        and validation.get("owned_command_receipts_validated") == owned_command_refs
    ):
        _fail(
            "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
            "owned-command validation census changed during seal",
        )
    for reference in owned_command_refs:
        _schema_validate(
            schemas["owned_command"],
            json.loads(_read_evidence_bytes(store, reference)),
            label="owned command",
        )
    _schema_validate(schemas["manifest"], manifest, label="manifest")
    _schema_validate(schemas["execution"], receipt, label="stored execution")
    raw_operation_ledger = receipt.get("operation_ledger")
    if type(raw_operation_ledger) is not dict:
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "operation ledger is absent")
    operation_ledger = cast(dict[str, JsonValue], raw_operation_ledger)
    if not (
        operation_ledger.get("owned_command_receipt_count") == len(owned_command_refs)
        and operation_ledger.get("owned_command_receipts") == owned_command_refs
    ):
        _fail(
            "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
            "operation ledger does not bind the owned-command receipt census",
        )
    manifest_subject = {
        key: value for key, value in manifest.items() if key != "manifest_subject_sha256"
    }
    receipt_subject = {
        key: value for key, value in receipt.items() if key != "receipt_subject_sha256"
    }
    if not (
        manifest.get("manifest_subject_sha256")
        == canonical_sha256(cast(JsonValue, manifest_subject))
        and receipt.get("receipt_subject_sha256")
        == canonical_sha256(cast(JsonValue, receipt_subject))
    ):
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "final subject self-hash differs")
    if manifest.get("exact_event_files") != store._event_files:
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "event-file census differs")
    listed_digests: set[str] = set()
    for raw_reference in cast(list[JsonValue], manifest["exact_content_objects"]):
        reference = cast(dict[str, JsonValue], raw_reference)
        _read_evidence_bytes(store, reference)
        listed_digests.add(cast(str, reference["sha256"]))
    explicit_self_exclusions = {
        cast(str, manifest_ref["sha256"]),
        cast(str, receipt_ref["sha256"]),
    }
    if not (
        manifest.get("self_excluded_content_object_roles")
        == ["MANIFEST_OBJECT", "TERMINAL_RECEIPT_OBJECT"]
        and manifest.get("self_excluded_content_object_count") == 2
        and manifest.get("content_object_census_rule")
        == "EXACT_PRESEAL_OBJECTS_PLUS_TWO_EXPLICIT_SELF_EXCLUSIONS"
        and listed_digests.isdisjoint(explicit_self_exclusions)
        and set(store._object_refs) == listed_digests | explicit_self_exclusions
    ):
        _fail(
            "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
            "content-object census does not close its two explicit self-exclusions",
        )


def _validate_runtime(value: object, keys: set[str], path: str) -> None:
    runtime = _closed(value, keys, path)
    python_path = _absolute_lexical(runtime.get("python_path"), f"{path}.python_path")
    resolved_path = _absolute_lexical(
        runtime.get("python_resolved_path"), f"{path}.python_resolved_path"
    )
    if python_path != resolved_path:
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "runtime Python path must be the exact resolved regular ELF path",
            f"{path}.python_resolved_path",
        )
    if not _is_sha256(runtime.get("python_sha256")):
        _fail("GPU_SMOKE_RUNTIME_INVALID", "python digest must be SHA-256", f"{path}.python_sha256")
    site_packages_path = _absolute_lexical(
        runtime.get("site_packages_path"), f"{path}.site_packages_path"
    )
    if "/site-packages" not in site_packages_path:
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "runtime site-packages path is not canonical",
            f"{path}.site_packages_path",
        )
    tree_digest = runtime.get("site_packages_tree_sha256")
    tree_entry_count = runtime.get("site_packages_tree_entry_count")
    tree_byte_count = runtime.get("site_packages_tree_byte_count")
    directory_mode = runtime.get("site_packages_directory_mode")
    regular_mode = runtime.get("site_packages_regular_mode")
    executable_mode = runtime.get("site_packages_executable_mode")
    if not (
        _is_sha256(tree_digest)
        and type(tree_entry_count) is int
        and cast(int, tree_entry_count) > 0
        and type(tree_byte_count) is int
        and cast(int, tree_byte_count) > 0
        and runtime.get("site_packages_owner_uid") == 0
        and runtime.get("site_packages_owner_gid") == 0
        and type(directory_mode) is int
        and cast(int, directory_mode) & 0o500 == 0o500
        and cast(int, directory_mode) & 0o022 == 0
        and type(regular_mode) is int
        and cast(int, regular_mode) & 0o400 == 0o400
        and cast(int, regular_mode) & 0o022 == 0
        and type(executable_mode) is int
        and cast(int, executable_mode) & 0o500 == 0o500
        and cast(int, executable_mode) & 0o022 == 0
        and runtime.get("site_packages_symlinks_allowed") is False
        and runtime.get("site_packages_hardlinks_allowed") is False
    ):
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "site-packages tree authority is not owner-exclusive and closed",
            path,
        )
    package_keys = keys - {
        "python_path",
        "python_resolved_path",
        "python_sha256",
        "site_packages_path",
        "site_packages_tree_sha256",
        "site_packages_tree_entry_count",
        "site_packages_tree_byte_count",
        "site_packages_owner_uid",
        "site_packages_owner_gid",
        "site_packages_directory_mode",
        "site_packages_regular_mode",
        "site_packages_executable_mode",
        "site_packages_symlinks_allowed",
        "site_packages_hardlinks_allowed",
    }
    for key in package_keys:
        if type(runtime.get(key)) is not str or not cast(str, runtime[key]):
            _fail(
                "GPU_SMOKE_RUNTIME_INVALID",
                "package version must be non-empty text",
                f"{path}.{key}",
            )


def _validate_bound_python_tree_authority(
    value: object,
    *,
    keys: set[str],
    path: str,
    outer: bool,
) -> dict[str, JsonValue]:
    runtime = _closed(value, keys, path)
    python_path = _absolute_lexical(runtime.get("python_path"), f"{path}.python_path")
    resolved_path = _absolute_lexical(
        runtime.get("python_resolved_path"),
        f"{path}.python_resolved_path",
    )
    root_key = "stdlib_root" if outer else "root"
    root = _absolute_lexical(runtime.get(root_key), f"{path}.{root_key}")
    stdlib_root = _absolute_lexical(runtime.get("stdlib_root"), f"{path}.stdlib_root")
    expected_version = "3.10.12" if outer else "3.12.12"
    expected_python = "/usr/bin/python3.10" if outer else f"{root}/bin/python3.12"
    expected_stdlib = "/usr/lib/python3.10" if outer else f"{root}/lib/python3.12"
    digest_key = "stdlib_tree_sha256" if outer else "tree_sha256"
    count_key = "stdlib_tree_entry_count" if outer else "tree_entry_count"
    bytes_key = "stdlib_tree_byte_count" if outer else "tree_byte_count"
    if not (
        python_path == resolved_path == expected_python
        and stdlib_root == expected_stdlib
        and _is_sha256(runtime.get("python_sha256"))
        and type(runtime.get("python_byte_count")) is int
        and cast(int, runtime["python_byte_count"]) > 0
        and runtime.get("python_version") == expected_version
        and runtime.get("python_flags") == list(_ISOLATED_PYTHON_FLAGS)
        and _is_sha256(runtime.get(digest_key))
        and type(runtime.get(count_key)) is int
        and cast(int, runtime[count_key]) > 0
        and type(runtime.get(bytes_key)) is int
        and cast(int, runtime[bytes_key]) > 0
    ):
        _fail("GPU_SMOKE_RUNTIME_INVALID", "bound Python tree authority differs", path)
    if outer:
        modes_and_owner_valid = (
            runtime.get("required_owner_uid") == 0
            and runtime.get("required_owner_gid") == 0
            and runtime.get("directory_mode") == 0o755
            and runtime.get("regular_mode") == 0o644
            and runtime.get("executable_mode") == 0o755
            and runtime.get("symlinks_allowed") is True
            and runtime.get("hardlinks_allowed") is True
        )
    else:
        modes_and_owner_valid = (
            runtime.get("owner_uid") == 0
            and runtime.get("owner_gid") == 0
            and runtime.get("directory_mode") == 0o500
            and runtime.get("regular_mode") == 0o400
            and runtime.get("executable_mode") == 0o500
            and runtime.get("symlinks_allowed") is False
            and runtime.get("hardlinks_allowed") is False
        )
    if not modes_and_owner_valid:
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "bound Python tree owner/group/mode policy differs",
            path,
        )
    return runtime


def _outer_fd_closure_receipt() -> dict[str, JsonValue]:
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-outer-fd-closure/v1",
        "strategy": "PYTHON_OS_CLOSERANGE_THEN_PROC_SELF_FD_REVALIDATION",
        "close_lower_bound": 3,
        "close_upper_bound_exclusive": _FD_CLOSE_UPPER_BOUND_EXCLUSIVE,
        "standard_fd_numbers": [0, 1, 2],
        "standard_fd_socket_count": 0,
        "standard_fds_non_inet": True,
        "remaining_fd_count_above_stderr": 0,
        "all_inherited_fds_closed": True,
        "forbidden_pre_unshare_module_count": 0,
        "foreign_process_fds_read": 0,
    }


def _validate_supplementary_group_authority(value: object) -> dict[str, JsonValue]:
    policy = _closed(
        value,
        _SUPPLEMENTARY_GROUP_KEYS,
        "$.network_namespace.supplementary_groups",
    )
    expected: dict[str, JsonValue] = {
        "schema_version": _SUPPLEMENTARY_GROUPS_SCHEMA_VERSION,
        "owner_approved": True,
        "policy": _SUPPLEMENTARY_GROUP_POLICY,
        "host_group_vector": _HOST_GROUP_VECTOR,
        "host_primary_gid": 1035,
        "host_supplementary_gids": _HOST_SUPPLEMENTARY_GIDS,
        "host_os_getgroups_sorted": [109, 999, 1035],
        "inside_supplementary_gids_sorted": _INSIDE_SUPPLEMENTARY_GIDS_SORTED,
        "inside_groups_empty_required": False,
        "setpriv_group_option": "--keep-groups",
        "setgroups_control_expected": "deny",
        "capability_sets_all_zero_required": True,
        "no_new_privs_required": True,
        "docker_group_gid": 999,
        "kvm_group_gid": 109,
        "docker_kvm_filesystem_access_allowed": False,
        "docker_kvm_socket_access_allowed": False,
        "docker_kvm_action_allowed": False,
        "docker_af_unix_capability_retained": True,
        "kvm_device_capability_retained": True,
        "docker_kvm_invocation_allowed": False,
        "docker_kvm_use_mechanically_proven_absent": False,
        "formal_supplementary_group_isolation_proven": False,
        "nonformal_residual_disclosed": True,
    }
    if canonical_json_bytes(cast(JsonValue, policy)) != canonical_json_bytes(expected):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
            "owner-approved retained supplementary-group policy differs",
            "$.network_namespace.supplementary_groups",
        )
    return policy


def _validate_tool_shell_component_binding(
    value: object,
    *,
    lexical_path: str,
    expected_paths: tuple[str, ...],
    path: str,
) -> dict[str, JsonValue]:
    binding = _closed(value, _TOOL_PATH_BINDING_KEYS, path)
    if binding.get("lexical_path") != lexical_path:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell lexical path differs", path)
    components = binding.get("component_bindings")
    if type(components) is not list or len(components) != len(expected_paths):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell component census differs", path)
    for index, expected_path in enumerate(expected_paths):
        component = _closed(
            cast(list[JsonValue], components)[index],
            _TOOL_PATH_COMPONENT_KEYS,
            f"{path}.component_bindings[{index}]",
        )
        expected_type, expected_target, expected_resolved = _TOOL_SHELL_COMPONENT_EXPECTED[
            expected_path
        ]
        if not (
            component.get("path") == expected_path
            and component.get("type") == expected_type
            and component.get("symlink_target") == expected_target
            and component.get("resolved_path") == expected_resolved
            and component.get("owner_uid") == 0
            and component.get("owner_gid") == 0
            and component.get("mode") == (0o777 if expected_type == "symlink" else 0o755)
            and type(component.get("nlink")) is int
            and cast(int, component["nlink"]) > 0
        ):
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "tool-shell component binding differs",
                f"{path}.component_bindings[{index}]",
            )
    return binding


def _validate_tool_shell_binary(
    value: object,
    *,
    lexical_path: str,
    resolved_path: str,
    sha256: str,
    byte_count: int,
    mode: int,
    path: str,
) -> dict[str, JsonValue]:
    binary = _closed(value, _TOOL_BINARY_KEYS, path)
    if binary != {
        "path": lexical_path,
        "resolved_path": resolved_path,
        "sha256": sha256,
        "byte_count": byte_count,
        "owner_uid": 0,
        "owner_gid": 0,
        "mode": mode,
        "nlink": 1,
    }:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell binary binding differs", path)
    return binary


def _validate_tool_shell_authority(value: object) -> dict[str, JsonValue]:
    tool_shell = _closed(value, _TOOL_SHELL_KEYS, "$.tool_shell")
    if tool_shell.get("schema_version") != TOOL_SHELL_SCHEMA_VERSION:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell schema version differs")
    _validate_tool_shell_component_binding(
        tool_shell.get("path_binding"),
        lexical_path=TOOL_SHELL_PATH,
        expected_paths=_TOOL_SHELL_PATH_COMPONENT_PATHS,
        path="$.tool_shell.path_binding",
    )
    _validate_tool_shell_binary(
        tool_shell.get("resolved_binary"),
        lexical_path=TOOL_SHELL_PATH,
        resolved_path=TOOL_SHELL_RESOLVED_PATH,
        sha256=TOOL_SHELL_SHA256,
        byte_count=TOOL_SHELL_BYTE_COUNT,
        mode=0o755,
        path="$.tool_shell.resolved_binary",
    )
    elf = _closed(tool_shell.get("elf"), _TOOL_SHELL_ELF_KEYS, "$.tool_shell.elf")
    if elf != {
        "machine": "EM_X86_64",
        "type": "ET_DYN",
        "elf_osabi": 0,
        "pt_interp": TOOL_SHELL_PT_INTERP_PATH,
        "dt_needed": ["libc.so.6"],
        "rpath_runpath_allowed": False,
        "bind_now": True,
        "pie": True,
        "nx_stack": True,
    }:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell ELF binding differs")
    _validate_tool_shell_component_binding(
        tool_shell.get("interpreter_path_binding"),
        lexical_path=TOOL_SHELL_PT_INTERP_PATH,
        expected_paths=_TOOL_SHELL_INTERPRETER_COMPONENT_PATHS,
        path="$.tool_shell.interpreter_path_binding",
    )
    _validate_tool_shell_binary(
        tool_shell.get("interpreter_binary"),
        lexical_path=TOOL_SHELL_PT_INTERP_PATH,
        resolved_path=TOOL_SHELL_PT_INTERP_RESOLVED_PATH,
        sha256=TOOL_SHELL_PT_INTERP_SHA256,
        byte_count=TOOL_SHELL_PT_INTERP_BYTE_COUNT,
        mode=0o755,
        path="$.tool_shell.interpreter_binary",
    )
    interpreter_elf = _closed(
        tool_shell.get("interpreter_elf"),
        _TOOL_INTERPRETER_ELF_KEYS,
        "$.tool_shell.interpreter_elf",
    )
    if interpreter_elf != {"machine": "EM_X86_64", "type": "ET_DYN", "elf_osabi": 3}:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell interpreter ELF differs")
    dependencies = tool_shell.get("dependencies")
    if type(dependencies) is not list or len(dependencies) != 1:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell dependency census differs")
    dependency = _closed(
        cast(list[JsonValue], dependencies)[0],
        _TOOL_DEPENDENCY_KEYS,
        "$.tool_shell.dependencies[0]",
    )
    if not (
        dependency.get("soname") == "libc.so.6"
        and dependency.get("elf_osabi") == 3
        and dependency.get("dt_needed") == ["ld-linux-x86-64.so.2"]
    ):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell dependency graph differs")
    _validate_tool_shell_binary(
        dependency.get("binary"),
        lexical_path=TOOL_SHELL_LIBC_PATH,
        resolved_path=TOOL_SHELL_LIBC_RESOLVED_PATH,
        sha256=TOOL_SHELL_LIBC_SHA256,
        byte_count=TOOL_SHELL_LIBC_BYTE_COUNT,
        mode=0o755,
        path="$.tool_shell.dependencies[0].binary",
    )
    _validate_tool_shell_binary(
        tool_shell.get("ld_so_cache"),
        lexical_path=TOOL_SHELL_LD_SO_CACHE_PATH,
        resolved_path=TOOL_SHELL_LD_SO_CACHE_PATH,
        sha256=TOOL_SHELL_LD_SO_CACHE_SHA256,
        byte_count=TOOL_SHELL_LD_SO_CACHE_BYTE_COUNT,
        mode=0o644,
        path="$.tool_shell.ld_so_cache",
    )
    preload = _closed(
        tool_shell.get("ld_so_preload"),
        _TOOL_LD_SO_PRELOAD_KEYS,
        "$.tool_shell.ld_so_preload",
    )
    if preload != {"path": TOOL_SHELL_LD_SO_PRELOAD_PATH, "present": False}:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "ld.so.preload binding differs")
    loader = _closed(
        tool_shell.get("loader_resolution"),
        _TOOL_LOADER_RESOLUTION_KEYS,
        "$.tool_shell.loader_resolution",
    )
    _validate_tool_shell_component_binding(
        loader.get("system_configuration_path_binding"),
        lexical_path="/etc",
        expected_paths=_TOOL_SHELL_SYSTEM_CONFIG_COMPONENT_PATHS,
        path="$.tool_shell.loader_resolution.system_configuration_path_binding",
    )
    _validate_tool_shell_component_binding(
        loader.get("ambient_path_binding"),
        lexical_path=TOOL_SHELL_AMBIENT_LD_LIBRARY_PATH,
        expected_paths=_TOOL_SHELL_AMBIENT_COMPONENT_PATHS,
        path="$.tool_shell.loader_resolution.ambient_path_binding",
    )
    if not (
        type(loader.get("ambient_tree_entry_count")) is int
        and cast(int, loader["ambient_tree_entry_count"]) > 0
        and _is_sha256(loader.get("ambient_tree_entry_census_sha256"))
        and loader.get("recursive_forbidden_soname_count") == 0
        and _is_sha256(loader.get("recursive_forbidden_soname_census_sha256"))
        and loader.get("ld_so_cache_selected_libc_path") == TOOL_SHELL_LIBC_PATH
        and loader.get("selected_libc_unique") is True
    ):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell loader resolution differs")
    ambient = _closed(
        tool_shell.get("ambient_environment"),
        _TOOL_AMBIENT_ENVIRONMENT_KEYS,
        "$.tool_shell.ambient_environment",
    )
    if ambient != {
        "required": {"LD_LIBRARY_PATH": TOOL_SHELL_AMBIENT_LD_LIBRARY_PATH},
        "forbidden_names": list(_TOOL_SHELL_FORBIDDEN_ENVIRONMENT_NAMES),
        "forbidden_prefixes": list(_TOOL_SHELL_FORBIDDEN_ENVIRONMENT_PREFIXES),
        "other_ld_environment_variables_allowed": False,
    }:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell ambient policy differs")
    command_prefix = (
        f"exec {LAUNCH_SHIM_PATH} -c {LAUNCH_SHIM_TOKEN_PREFIX}:{LAUNCH_SHIM_AUTHORITY_PATH}:"
    )
    invocation = _closed(
        tool_shell.get("invocation"),
        _TOOL_INVOCATION_KEYS,
        "$.tool_shell.invocation",
    )
    if invocation != {
        "shell_option": "-c",
        "login": False,
        "tty": False,
        "command_grammar": "EXEC_ABSOLUTE_SHIM_COLON_TOKEN_V2",
        "command_prefix": command_prefix,
        "command_prefix_sha256": _sha256(command_prefix.encode("ascii")),
        "command_prefix_byte_count": len(command_prefix.encode("ascii")),
        "command_authority_sha256_byte_count": 64,
        "command_total_byte_count": len(command_prefix.encode("ascii")) + 64,
    }:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell invocation differs")
    claims = _closed(
        tool_shell.get("formal_claims"),
        _TOOL_FORMAL_CLAIM_KEYS,
        "$.tool_shell.formal_claims",
    )
    if claims != {
        "direct_exec_formally_proven": False,
        "pre_gate_dynamic_loader_closure_formally_proven": False,
    }:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell formal claims differ")
    return tool_shell


def _validate_authority(value: dict[str, JsonValue], canonical: bytes) -> GpuLiveAuthority:
    authority = _closed(value, _AUTHORITY_KEYS, "$")
    if authority.get("schema_version") != AUTHORITY_SCHEMA_VERSION:
        _fail("GPU_SMOKE_AUTHORITY_VERSION_INVALID", "authority schema version differs")
    authority_id = authority.get("authority_id")
    if type(authority_id) is not str or _ID_RE.fullmatch(cast(str, authority_id)) is None:
        _fail("GPU_SMOKE_AUTHORITY_ID_INVALID", "authority ID is not canonical", "$.authority_id")
    if not (
        authority.get("decision_id") == DECISION_ID
        and authority.get("authorized_scope") == AUTHORIZED_SCOPE
        and authority.get("authorized") is True
    ):
        _fail("GPU_SMOKE_AUTHORITY_SCOPE_INVALID", "authority does not grant the exact D-034 scope")
    issued = _parse_utc(authority.get("issued_at_utc"), "$.issued_at_utc")
    expires = _parse_utc(authority.get("expires_at_utc"), "$.expires_at_utc")
    if issued >= expires:
        _fail("GPU_SMOKE_AUTHORITY_TIME_INVALID", "authority interval is empty")
    if type(authority.get("owner_uid")) is not int or authority.get("owner_uid") != 0:
        _fail(
            "GPU_SMOKE_AUTHORITY_OWNER_INVALID",
            "the isolated execution owner UID must be exact namespace UID 0",
        )

    gpu = _closed(authority.get("gpu"), _GPU_KEYS, "$.gpu")
    gpu_uuid = gpu.get("uuid")
    if not (
        gpu.get("physical_index") == 0
        and gpu_uuid == AUTHORIZED_GPU_UUID
        and gpu.get("cuda_visible_devices") == gpu_uuid
        and gpu.get("shared") is True
        and gpu.get("exclusive") is False
        and gpu.get("minimum_free_memory_bytes") == MINIMUM_FREE_MEMORY_BYTES
        and gpu.get("foreign_process_signaling_allowed") is False
    ):
        _fail(
            "GPU_SMOKE_GPU_AUTHORITY_INVALID",
            "GPU authority must bind shared physical GPU 0 by exact UUID",
            "$.gpu",
        )
    if authority.get("endpoint") != cast(JsonValue, EXACT_ENDPOINT):
        _fail(
            "GPU_SMOKE_ENDPOINT_INVALID",
            "only the frozen loopback endpoint is allowed",
            "$.endpoint",
        )
    if authority.get("model_order") != list(MODEL_ORDER):
        _fail("GPU_SMOKE_MODEL_ORDER_INVALID", "models must run Qwen then MAI", "$.model_order")
    models = _closed(authority.get("models"), set(MODEL_ORDER), "$.models")
    for model_id in MODEL_ORDER:
        model = _closed(models.get(model_id), _MODEL_AUTH_KEYS, f"$.models.{model_id}")
        identity = MODEL_IDENTITIES[model_id]
        if any(model.get(key) != expected for key, expected in identity.items()):
            _fail(
                "GPU_SMOKE_MODEL_BINDING_INVALID",
                "model identity differs from the frozen manifest",
                f"$.models.{model_id}",
            )
        if not (
            _is_sha256(model.get("snapshot_tree_sha256"))
            and type(model.get("snapshot_tree_entry_count")) is int
            and cast(int, model["snapshot_tree_entry_count"]) > 0
            and type(model.get("snapshot_tree_byte_count")) is int
            and cast(int, model["snapshot_tree_byte_count"]) > 0
        ):
            _fail(
                "GPU_SMOKE_MODEL_BINDING_INVALID",
                "complete snapshot tree authority is invalid",
                f"$.models.{model_id}",
            )
        snapshot = _absolute_lexical(
            model.get("snapshot_path"), f"$.models.{model_id}.snapshot_path"
        )
        expected_tail = (
            f"models--{cast(str, identity['repository']).replace('/', '--')}",
            "snapshots",
            cast(str, identity["revision"]),
        )
        if tuple(PurePosixPath(snapshot).parts[-3:]) != expected_tail:
            _fail(
                "GPU_SMOKE_MODEL_BINDING_INVALID",
                "snapshot path does not bind repository revision",
                f"$.models.{model_id}.snapshot_path",
            )

    outer_runtime = _validate_bound_python_tree_authority(
        authority.get("outer_runtime"),
        keys=_OUTER_RUNTIME_KEYS,
        path="$.outer_runtime",
        outer=True,
    )
    private_runtime = _validate_bound_python_tree_authority(
        authority.get("private_runtime"),
        keys=_PRIVATE_RUNTIME_KEYS,
        path="$.private_runtime",
        outer=False,
    )
    _validate_runtime(authority.get("client_runtime"), _RUNTIME_CLIENT_KEYS, "$.client_runtime")
    _validate_runtime(authority.get("server_runtime"), _RUNTIME_SERVER_KEYS, "$.server_runtime")
    client_runtime = cast(dict[str, JsonValue], authority["client_runtime"])
    server_runtime = cast(dict[str, JsonValue], authority["server_runtime"])
    if not (
        client_runtime.get("python_path") == private_runtime["python_path"]
        and server_runtime.get("python_path") == private_runtime["python_path"]
        and client_runtime.get("python_sha256") == private_runtime["python_sha256"]
        and server_runtime.get("python_sha256") == private_runtime["python_sha256"]
        and outer_runtime["python_path"] != private_runtime["python_path"]
    ):
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "client/server must share only the authority-sealed private Python ELF",
            "$.private_runtime",
        )
    private_root = cast(str, private_runtime["root"])
    if not (
        client_runtime.get("site_packages_path") == f"{private_root}/site-packages/client"
        and server_runtime.get("site_packages_path") == f"{private_root}/site-packages/server"
        and client_runtime.get("site_packages_directory_mode") == 0o500
        and client_runtime.get("site_packages_regular_mode") == 0o400
        and client_runtime.get("site_packages_executable_mode") == 0o500
        and server_runtime.get("site_packages_directory_mode") == 0o500
        and server_runtime.get("site_packages_regular_mode") == 0o400
        and server_runtime.get("site_packages_executable_mode") == 0o500
    ):
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "client/server site-packages must be separate sealed private-runtime subtrees",
            "$.private_runtime",
        )
    if client_runtime.get("openai_version") != "1.106.1":
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "client must use frozen OpenAI SDK 1.106.1",
            "$.client_runtime.openai_version",
        )
    if not (
        server_runtime.get("openai_version") == "2.15.0"
        and server_runtime.get("vllm_version") == "0.11.0"
        and server_runtime.get("torch_version") == "2.8.0+cu126"
    ):
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "server runtime versions differ from frozen serving environment",
            "$.server_runtime",
        )

    bindings = _closed(authority.get("bindings"), _BINDING_KEYS, "$.bindings")
    digest_bindings = _BINDING_KEYS - {"source_git_commit"}
    if not all(_is_sha256(bindings.get(key)) for key in digest_bindings):
        _fail("GPU_SMOKE_BINDING_INVALID", "authority digest binding is invalid", "$.bindings")
    source_commit = bindings.get("source_git_commit")
    if type(source_commit) is not str or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        _fail("GPU_SMOKE_BINDING_INVALID", "source commit binding is invalid", "$.bindings")
    if not (
        bindings.get("model_config_manifest_sha256") == MODEL_CONFIG_MANIFEST_SHA256
        and bindings.get("live_preparation_receipt_sha256") == LIVE_PREPARATION_RECEIPT_SHA256
        and bindings.get("g1_5_cpu_publication_sha256") == G1_5_CPU_PUBLICATION_SHA256
    ):
        _fail("GPU_SMOKE_BINDING_INVALID", "frozen upstream binding differs", "$.bindings")

    matrix = _closed(authority.get("matrix"), _MATRIX_KEYS, "$.matrix")
    if not (
        matrix.get("total_calls") == 22
        and matrix.get("g1_4_calls") == 12
        and matrix.get("g1_5_calls") == 10
        and matrix.get("replay_seeds") == list(REPLAY_SEEDS)
        and matrix.get("repeats_per_seed") == 2
        and matrix.get("g1_5_seed") == 1729
        and matrix.get("arms") == list(G1_5_ARMS)
    ):
        _fail(
            "GPU_SMOKE_MATRIX_AUTHORITY_INVALID",
            "authority matrix differs from the frozen 22-call proof",
            "$.matrix",
        )

    policies = _closed(authority.get("policies"), _POLICY_KEYS, "$.policies")
    expected_policies: dict[str, JsonValue] = {
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
    }
    if policies != expected_policies:
        _fail("GPU_SMOKE_POLICY_INVALID", "authority safety policy differs", "$.policies")

    source = _closed(authority.get("source"), _SOURCE_AUTH_KEYS, "$.source")
    worktree_root = _absolute_lexical(source.get("worktree_root"), "$.source.worktree_root")
    source_root = _absolute_lexical(source.get("source_root"), "$.source.source_root")
    if source_root != f"{worktree_root}/MobileWorld/src":
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "source root is not the exact worktree MobileWorld source directory",
            "$.source.source_root",
        )
    if not (
        source.get("git_path") == "/usr/bin/git"
        and _is_sha256(source.get("git_sha256"))
        and source.get("head_commit") == bindings["source_git_commit"]
        and _is_sha256(source.get("source_tree_sha256"))
        and type(source.get("source_tree_entry_count")) is int
        and cast(int, source["source_tree_entry_count"]) > 0
        and type(source.get("source_tree_byte_count")) is int
        and cast(int, source["source_tree_byte_count"]) > 0
        and _is_sha256(source.get("outer_bootstrap_code_sha256"))
        and type(source.get("outer_bootstrap_code_byte_count")) is int
        and cast(int, source["outer_bootstrap_code_byte_count"]) > 0
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "source Git binding differs from the frozen tool/commit",
            "$.source",
        )
    critical_files = _closed(
        source.get("critical_files"),
        set(_CRITICAL_SOURCE_FILES),
        "$.source.critical_files",
    )
    for name, relative_path in _CRITICAL_SOURCE_FILES.items():
        binding = _closed(
            critical_files.get(name),
            _SOURCE_FILE_BINDING_KEYS,
            f"$.source.critical_files.{name}",
        )
        if not (
            binding.get("relative_path") == relative_path and _is_sha256(binding.get("sha256"))
        ):
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "critical source file binding is invalid",
                f"$.source.critical_files.{name}",
            )
    bootstrap_subject: dict[str, JsonValue] = {
        "worktree_root": worktree_root,
        "source_root": source_root,
        "client_site_packages_path": client_runtime["site_packages_path"],
        "server_site_packages_path": server_runtime["site_packages_path"],
        "python_flags": list(_ISOLATED_PYTHON_FLAGS),
        "python_pycache_prefix": "/dev/null",
        "server_bootstrap_code_sha256": _sha256(
            _server_bootstrap_code(cast(str, server_runtime["site_packages_path"])).encode("utf-8")
        ),
        "critical_files": critical_files,
        "source_tree_sha256": source["source_tree_sha256"],
        "source_tree_entry_count": source["source_tree_entry_count"],
        "source_tree_byte_count": source["source_tree_byte_count"],
        "outer_bootstrap_code_sha256": source["outer_bootstrap_code_sha256"],
        "outer_bootstrap_code_byte_count": source["outer_bootstrap_code_byte_count"],
    }
    if source.get("bootstrap_manifest_sha256") != canonical_sha256(bootstrap_subject):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "source bootstrap manifest digest differs",
            "$.source.bootstrap_manifest_sha256",
        )

    launch_shim = _closed(authority.get("launch_shim"), _LAUNCH_SHIM_KEYS, "$.launch_shim")
    expected_shim_source_path = f"{worktree_root}/{_CRITICAL_SOURCE_FILES['launch_shim_source']}"
    expected_runner_cli_path = f"{worktree_root}/{_CRITICAL_SOURCE_FILES['runner_cli']}"
    expected_packet_path = (
        "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/"
        "d034-9845577c/packet-objects/objects/sha256/"
        f"{cast(str, bindings['smoke_packet_sha256'])[:2]}/"
        f"{bindings['smoke_packet_sha256']}"
    )
    expected_manifest_path = (
        f"{worktree_root}/mobileworld_audit_handoff/g1/model_config_manifest.v1.json"
    )
    shim_source_binding = cast(dict[str, JsonValue], critical_files["launch_shim_source"])
    if not (
        launch_shim.get("schema_version") == LAUNCH_SHIM_SCHEMA_VERSION
        and launch_shim.get("path") == LAUNCH_SHIM_PATH
        and launch_shim.get("resolved_path") == LAUNCH_SHIM_PATH
        and _is_sha256(launch_shim.get("sha256"))
        and type(launch_shim.get("byte_count")) is int
        and cast(int, launch_shim["byte_count"]) > 0
        and launch_shim.get("owner_uid") == 1035
        and launch_shim.get("owner_gid") == 1035
        and launch_shim.get("mode") == 0o500
        and launch_shim.get("nlink") == 1
        and launch_shim.get("source_path") == expected_shim_source_path
        and launch_shim.get("source_sha256") == shim_source_binding["sha256"]
        and launch_shim.get("shell_option") == "-c"
        and launch_shim.get("token_prefix") == LAUNCH_SHIM_TOKEN_PREFIX
        and launch_shim.get("runner_cli_path") == expected_runner_cli_path
        and launch_shim.get("smoke_packet_path") == expected_packet_path
        and launch_shim.get("model_config_manifest_path") == expected_manifest_path
        and launch_shim.get("bootstrap_sha256") == source["outer_bootstrap_code_sha256"]
        and launch_shim.get("bootstrap_byte_count") == source["outer_bootstrap_code_byte_count"]
        and launch_shim.get("confirmation") == "EXECUTE-D034-SYNTHETIC-22-CALL-SMOKE"
        and launch_shim.get("elf_machine") == "EM_X86_64"
        and launch_shim.get("elf_type") == "ET_EXEC"
        and launch_shim.get("static") is True
        and launch_shim.get("pt_interp_allowed") is False
        and launch_shim.get("pt_dynamic_allowed") is False
        and launch_shim.get("dt_needed_allowed") is False
        and launch_shim.get("rpath_runpath_allowed") is False
        and launch_shim.get("init_array_allowed") is False
        and launch_shim.get("fini_array_allowed") is False
        and launch_shim.get("tls_segment_allowed") is False
        and launch_shim.get("writable_executable_segment_allowed") is False
        and launch_shim.get("executable_stack") is False
        and bindings["runner_cli_sha256"]
        == cast(dict[str, JsonValue], critical_files["runner_cli"])["sha256"]
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "static pre-gate launch shim binding differs",
            "$.launch_shim",
        )
    _validate_tool_shell_authority(authority.get("tool_shell"))

    evidence_root = _absolute_lexical(authority.get("evidence_root"), "$.evidence_root")
    scratch_root = _absolute_lexical(
        authority.get("runtime_scratch_root"), "$.runtime_scratch_root"
    )
    if not (evidence_root == EVIDENCE_ROOT_V4 and scratch_root == RUNTIME_SCRATCH_ROOT_V4):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "v4 authority must use the fresh evidence and runtime-scratch roots",
            "$.evidence_root",
        )
    protected_paths: list[tuple[str, PurePosixPath]] = [
        ("repository", PurePosixPath(worktree_root)),
        ("evidence", PurePosixPath(evidence_root)),
        ("runtime_scratch", PurePosixPath(scratch_root)),
        ("private_runtime", PurePosixPath(cast(str, private_runtime["root"]))),
        *(
            (
                f"snapshot:{model_id}",
                PurePosixPath(
                    cast(str, cast(dict[str, JsonValue], models[model_id])["snapshot_path"])
                ),
            )
            for model_id in MODEL_ORDER
        ),
    ]
    for ordinal, (left_name, left_path) in enumerate(protected_paths):
        for right_name, right_path in protected_paths[ordinal + 1 :]:
            if (
                left_path == right_path
                or left_path in right_path.parents
                or right_path in left_path.parents
            ):
                _fail(
                    "GPU_SMOKE_PROTECTED_ROOT_OVERLAP",
                    (
                        "repository, evidence, runtime scratch, and model snapshots must be "
                        f"pairwise disjoint ({left_name} overlaps {right_name})"
                    ),
                    "$.evidence_root",
                )

    namespace = _closed(
        authority.get("network_namespace"),
        _NETWORK_NAMESPACE_KEYS,
        "$.network_namespace",
    )
    pre_namespace_environment = _closed(
        namespace.get("pre_namespace_environment"),
        _PRE_NAMESPACE_ENVIRONMENT_KEYS,
        "$.network_namespace.pre_namespace_environment",
    )
    if pre_namespace_environment != {"LC_CTYPE": "C.UTF-8"}:
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
            "pre-namespace environment is not the exact loader-minimal allowlist",
            "$.network_namespace.pre_namespace_environment",
        )
    for key, expected_path in (
        ("env_path", "/usr/bin/env"),
        ("unshare_path", "/usr/bin/unshare"),
        ("ip_path", "/usr/bin/ip"),
        ("setpriv_path", "/usr/bin/setpriv"),
    ):
        if namespace.get(key) != expected_path:
            _fail(
                "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
                "namespace launcher path differs from the frozen absolute path",
                f"$.network_namespace.{key}",
            )
        if not _is_sha256(namespace.get(key.replace("_path", "_sha256"))):
            _fail(
                "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
                "namespace launcher digest is invalid",
                f"$.network_namespace.{key.replace('_path', '_sha256')}",
            )
    if not (
        namespace.get("nvidia_smi_path") == "/usr/bin/nvidia-smi"
        and _is_sha256(namespace.get("nvidia_smi_sha256"))
        and type(namespace.get("nvidia_smi_byte_count")) is int
        and cast(int, namespace["nvidia_smi_byte_count"]) > 0
    ):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
            "nvidia-smi executable binding is invalid",
            "$.network_namespace.nvidia_smi_path",
        )
    host_uid = namespace.get("host_owner_uid")
    host_gid = namespace.get("host_owner_gid")
    if not (
        namespace.get("required") is True
        and namespace.get("implementation") == "LINUX_USER_NETNS_MAP_ROOT_RETAIN_GROUPS_V2"
        and type(host_uid) is int
        and host_uid == 1035
        and type(host_gid) is int
        and host_gid == 1035
        and type(namespace.get("inside_owner_uid")) is int
        and namespace.get("inside_owner_uid") == 0
        and type(namespace.get("inside_owner_gid")) is int
        and namespace.get("inside_owner_gid") == 0
        and namespace.get("inside_unmapped_system_uid") == 65_534
        and namespace.get("inside_unmapped_system_gid") == 65_534
        and namespace.get("uid_map_line") == f"0 {host_uid} 1"
        and namespace.get("gid_map_line") == f"0 {host_gid} 1"
        and namespace.get("expected_interfaces") == ["lo"]
        and namespace.get("loopback_up_required") is True
        and namespace.get("default_route_allowed") is False
        and namespace.get("external_network_allowed") is False
        and namespace.get("python_pycache_prefix") == "/dev/null"
        and namespace.get("fd_close_upper_bound_exclusive") == _FD_CLOSE_UPPER_BOUND_EXCLUSIVE
        and namespace.get("outer_fd_closure_receipt_sha256")
        == canonical_sha256(_outer_fd_closure_receipt())
    ):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
            "network namespace authority differs from the frozen root mapping",
            "$.network_namespace",
        )
    _validate_supplementary_group_authority(namespace.get("supplementary_groups"))
    launcher_environment = _closed(
        namespace.get("launcher_environment"),
        _LAUNCHER_ENVIRONMENT_KEYS,
        "$.network_namespace.launcher_environment",
    )
    expected_launcher_environment: dict[str, JsonValue] = {
        "PATH": "/usr/bin:/bin",
        "CUDA_VISIBLE_DEVICES": AUTHORIZED_GPU_UUID,
        "LD_LIBRARY_PATH": "",
        "HOME": f"{scratch_root}/namespace-launcher/home",
        "HF_HOME": f"{scratch_root}/namespace-launcher/hf-home",
        "XDG_CACHE_HOME": f"{scratch_root}/namespace-launcher/xdg-cache",
        "TORCH_HOME": f"{scratch_root}/namespace-launcher/torch-home",
        "TRITON_CACHE_DIR": f"{scratch_root}/namespace-launcher/triton-cache",
        "VLLM_CACHE_ROOT": f"{scratch_root}/namespace-launcher/vllm-cache",
        "TMPDIR": f"{scratch_root}/namespace-launcher/tmp",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "GPU_SMOKE_OUTER_FD_CLOSURE_SHA256": namespace.get("outer_fd_closure_receipt_sha256"),
    }
    ld_library_path = launcher_environment.get("LD_LIBRARY_PATH")
    if ld_library_path != "":
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
            "launcher LD_LIBRARY_PATH must be the exact empty string",
            "$.network_namespace.launcher_environment.LD_LIBRARY_PATH",
        )
    if launcher_environment != expected_launcher_environment:
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
            "launcher environment differs from the exact secret-free allowlist",
            "$.network_namespace.launcher_environment",
        )
    _scan_for_secrets(cast(JsonValue, authority))
    return GpuLiveAuthority(authority, canonical, _sha256(canonical))


def load_gpu_live_authority(path: str | os.PathLike[str], expected_sha256: str) -> GpuLiveAuthority:
    """Load a canonical, no-follow authority bound by an out-of-band digest."""

    if not _is_sha256(expected_sha256):
        _fail("GPU_SMOKE_AUTHORITY_HASH_INVALID", "expected authority digest is invalid")
    value, data = _read_json_nofollow(path, maximum_bytes=128 * 1024)
    if _sha256(data) != expected_sha256:
        _fail("GPU_SMOKE_AUTHORITY_HASH_MISMATCH", "authority digest differs")
    return _validate_authority(value, data)


def _validate_call(
    value: object,
    *,
    index: int,
    expected: tuple[str, str, int, int | None, str | None],
) -> GpuSmokeCall:
    path = f"$.calls[{index}]"
    call = _closed(value, _CALL_KEYS, path)
    model_id, phase, seed, repeat_index, arm = expected
    tag = "qwen" if model_id == "qwen3vl_8b" else "mai"
    expected_call_id = (
        f"g14-{tag}-s{seed}-r{repeat_index}"
        if phase == "G1_4_CANARY"
        else f"g15-{tag}-{cast(str, arm).lower().replace('_', '-')}-s{seed}"
    )
    if not (
        call.get("call_id") == expected_call_id
        and call.get("model_id") == model_id
        and call.get("phase") == phase
        and call.get("seed") == seed
        and call.get("repeat_index") == repeat_index
        and call.get("arm") == arm
    ):
        _fail("GPU_SMOKE_MATRIX_INVALID", "call identity/order differs from frozen matrix", path)
    application_request = call.get("application_request")
    if type(application_request) is not dict:
        _fail(
            "GPU_SMOKE_REQUEST_INVALID",
            "application request must be an object",
            f"{path}.application_request",
        )
    try:
        request_bytes = canonical_json_bytes(cast(JsonValue, application_request))
    except Exception as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_REQUEST_INVALID", "request is not canonical JSON", json_path=path
        ) from exc
    if phase == "G1_4_CANARY":
        if not (
            call.get("codec_id") is None
            and call.get("diff") is None
            and call.get("mapping") is None
            and call.get("render_evidence") is None
        ):
            _fail(
                "GPU_SMOKE_CANARY_SHAPE_INVALID", "canary may not carry codec/render evidence", path
            )
    else:
        if call.get("codec_id") != MODEL_CODECS[model_id]:
            _fail("GPU_SMOKE_CODEC_BINDING_INVALID", "codec ID differs", f"{path}.codec_id")
        if type(call.get("diff")) is not list or type(call.get("mapping")) is not dict:
            _fail("GPU_SMOKE_RENDER_EVIDENCE_INVALID", "G1.5 diff/mapping shape is invalid", path)
        evidence = _closed(
            call.get("render_evidence"), _RENDER_EVIDENCE_KEYS, f"{path}.render_evidence"
        )
        if not (
            _is_sha256(evidence.get("source_application_request_sha256"))
            and evidence.get("rendered_application_request_sha256") == _sha256(request_bytes)
            and evidence.get("diff_sha256")
            == _sha256(canonical_json_bytes(cast(JsonValue, call["diff"])))
            and evidence.get("mapping_sha256")
            == _sha256(canonical_json_bytes(cast(JsonValue, call["mapping"])))
            and evidence.get("target_only_diff") is True
            and evidence.get("source_mapping_reversible") is True
            and evidence.get("provider_invocation_allowed") is False
        ):
            _fail(
                "GPU_SMOKE_RENDER_EVIDENCE_INVALID",
                "render evidence hash/invariant differs",
                f"{path}.render_evidence",
            )
    return GpuSmokeCall(call, request_bytes)


def _expected_matrix(g1_5_seed: int) -> tuple[tuple[str, str, int, int | None, str | None], ...]:
    expected: list[tuple[str, str, int, int | None, str | None]] = []
    for model_id in MODEL_ORDER:
        for seed in REPLAY_SEEDS:
            for repeat in (1, 2):
                expected.append((model_id, "G1_4_CANARY", seed, repeat, None))
        for arm in G1_5_ARMS:
            expected.append((model_id, "G1_5_CODEC", g1_5_seed, None, arm))
    return tuple(expected)


def _read_frozen_fixture(path: str | os.PathLike[str], model_id: str) -> dict[str, JsonValue]:
    raw_path = _absolute_lexical(os.fspath(path), f"$.fixtures.{model_id}")
    expected = _FIXTURES[model_id]
    if not raw_path.endswith("/" + cast(str, expected["relative_path"])):
        _fail(
            "GPU_SMOKE_FIXTURE_PATH_INVALID",
            "fixture path does not end in the frozen repository-relative path",
            f"$.fixtures.{model_id}",
        )
    try:
        fd = os.open(raw_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_FIXTURE_UNREADABLE", "fixture could not be opened safely"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > 2_000_000:
            _fail("GPU_SMOKE_FIXTURE_UNSAFE", "fixture is not a bounded regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or len(data) != before.st_size
        or _sha256(data) != expected["file_sha256"]
    ):
        _fail("GPU_SMOKE_FIXTURE_HASH_MISMATCH", "frozen fixture bytes differ")
    try:
        value = json.loads(data, object_pairs_hook=_duplicate_rejecting_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GpuLiveSmokeError("GPU_SMOKE_FIXTURE_INVALID", "fixture is not strict JSON") from exc
    if type(value) is not dict:
        _fail("GPU_SMOKE_FIXTURE_INVALID", "fixture root must be an object")
    fixture = cast(dict[str, JsonValue], value)
    if not (
        fixture.get("fixture_id") == expected["fixture_id"]
        and fixture.get("fixture_request_sha256") == expected["fixture_request_sha256"]
        and isinstance(fixture.get("sanitization"), dict)
        and cast(dict[str, JsonValue], fixture["sanitization"]).get("performed") is True
        and cast(dict[str, JsonValue], fixture["sanitization"]).get("source_bytes_copied") is False
        and cast(dict[str, JsonValue], fixture["sanitization"]).get("fixture_is_formal_g1_data")
        is False
    ):
        _fail("GPU_SMOKE_FIXTURE_INVALID", "fixture identity/sanitization differs")
    request = fixture.get("application_request")
    if (
        type(request) is not dict
        or canonical_sha256(cast(JsonValue, request)) != expected["fixture_request_sha256"]
    ):
        _fail("GPU_SMOKE_FIXTURE_INVALID", "fixture request digest differs")
    return fixture


def _fixture_bindings(fixture: dict[str, JsonValue]) -> tuple[CuratedSpanBinding, ...]:
    raw = fixture.get("curated_span_bindings")
    if type(raw) is not list:
        _fail("GPU_SMOKE_FIXTURE_INVALID", "curated span bindings are missing")
    result: list[CuratedSpanBinding] = []
    try:
        for item_value in cast(list[JsonValue], raw):
            item = cast(dict[str, JsonValue], item_value)
            result.append(
                CuratedSpanBinding(
                    binding_id=cast(str, item["binding_id"]),
                    source_request_sha256=cast(str, item["source_request_sha256"]),
                    container_path=tuple(cast(list[str | int], item["container_path"])),
                    char_start=cast(int, item["char_start"]),
                    char_end=cast(int, item["char_end"]),
                    utf8_byte_start=cast(int, item["utf8_byte_start"]),
                    utf8_byte_end=cast(int, item["utf8_byte_end"]),
                    exact_text=cast(str, item["exact_text"]),
                    span_sha256=cast(str, item["span_sha256"]),
                    span_role=SpanRole(cast(str, item["span_role"])),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_FIXTURE_INVALID", "curated span binding is malformed"
        ) from exc
    return tuple(result)


def _fixture_targets(ir: object) -> dict[str, tuple[object, object]]:
    found: dict[str, tuple[object, object]] = {}
    for record in getattr(ir, "records"):
        raw_ids = record.provenance.get("curated_binding_ids")
        if type(raw_ids) is not list or len(raw_ids) != len(record.editable_spans):
            _fail("GPU_SMOKE_FIXTURE_INVALID", "codec binding provenance differs")
        for binding_id, span in zip(raw_ids, record.editable_spans, strict=True):
            found[cast(str, binding_id)] = (record, span)
    return found


def _fixture_plan(
    ir: object,
    arm: ArmKind,
    binding_ids: list[str],
    correction_text: str,
) -> TransformationPlan:
    targets = _fixture_targets(ir)
    operations: list[PlanOperation] = []
    for index, binding_id in enumerate(binding_ids):
        try:
            record, span = targets[binding_id]
        except KeyError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_FIXTURE_INVALID", "plan target is not in extracted History IR"
            ) from exc
        operation_id = f"g15-{arm.value.lower()}-{index:02d}-{binding_id}"
        if arm is ArmKind.MASK_CORRECTION:
            anchor = getattr(record, "correction_anchors")[0]
            operation = PlanOperation(
                operation_id=operation_id,
                kind=OperationKind.REPLACE,
                target_record_id=getattr(record, "record_id"),
                target_span=span,
                replacement_text=correction_text,
                replacement_author="SENTINEL",
                evidence_refs=(
                    EvidenceRef(
                        evidence_id="g15-secret-free-pre-cutoff-evidence",
                        sha256="e" * 64,
                        role="current_observation_pre_cutoff",
                        event_seq=7,
                    ),
                ),
                correction_anchor=anchor,
                rendered_correction_context={
                    "type": "text",
                    "text": f"{anchor.visible_prefix}{correction_text}{anchor.visible_suffix}",
                },
            )
        else:
            operation = PlanOperation(
                operation_id=operation_id,
                kind=OperationKind.DROP,
                target_record_id=getattr(record, "record_id"),
                target_span=span,
            )
        operations.append(operation)
    operations.sort(
        key=lambda item: (
            tuple(str(token) for token in item.target_span.container_path),
            item.target_span.char_start,
            item.target_span.char_end,
            item.operation_id,
        )
    )
    subject: dict[str, JsonValue] = {
        "host_id": getattr(ir, "host_id"),
        "history_family": getattr(ir, "history_family").value,
        "codec_id": getattr(ir, "codec_id"),
        "codec_contract_version": getattr(ir, "codec_contract_version"),
        "source_request_sha256": getattr(ir, "raw_request_sha256"),
        "arm": arm.value,
        "operations": [item.to_dict() for item in operations],
    }
    return TransformationPlan(
        plan_id=stable_id("plan", subject),
        host_id=cast(str, subject["host_id"]),
        history_family=getattr(ir, "history_family"),
        codec_id=cast(str, subject["codec_id"]),
        codec_contract_version=cast(str, subject["codec_contract_version"]),
        source_request_sha256=cast(str, subject["source_request_sha256"]),
        arm=arm,
        operations=tuple(operations),
        curated=True,
        deployment_prediction=False,
    )


def _compile_model_requests(
    model_id: str, fixture: dict[str, JsonValue]
) -> dict[str, tuple[dict[str, JsonValue], list[JsonValue], dict[str, JsonValue]]]:
    bindings = _fixture_bindings(fixture)
    codec = (
        QwenFlatProgressHistoryCodec(bindings)
        if model_id == "qwen3vl_8b"
        else MaiRawReplayHistoryCodec(bindings)
    )
    source = cast(dict[str, JsonValue], fixture["application_request"])
    ir = codec.extract(source)
    targets = cast(dict[str, JsonValue], fixture["plan_targets"])
    correction = cast(str, fixture["correction_text"])
    by_arm_ids = {
        ArmKind.ORIGINAL: [],
        ArmKind.MASK: cast(list[str], targets["mask"]),
        ArmKind.MASK_CORRECTION: cast(list[str], targets["mask_correction"]),
        ArmKind.ORACLE_CLEAN: cast(list[str], targets["oracle_clean"]),
        ArmKind.SHAM_BENIGN_EDIT: cast(list[str], targets["sham_benign_edit"]),
    }
    plans = tuple(_fixture_plan(ir, arm, by_arm_ids[arm], correction) for arm in ArmKind)
    registry = HistoryCodecRegistry()
    registry.register(codec)
    validate_plan_set(
        source,
        ir,
        plans,
        codec_registry=registry,
        codec_contract_version=codec.contract_version,
        plan_set_profile=PlanSetProfile.G1_STRICT_MHR,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    compiled: dict[str, tuple[dict[str, JsonValue], list[JsonValue], dict[str, JsonValue]]] = {}
    expected_hashes = cast(dict[str, JsonValue], fixture["expected_rendered_request_sha256"])
    for arm, plan in zip(ArmKind, plans, strict=True):
        result = codec.render(
            source,
            ir,
            plan,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
        if result.rendered_request_sha256 != expected_hashes[arm.value]:
            _fail("GPU_SMOKE_FIXTURE_RENDER_MISMATCH", "codec render differs from frozen fixture")
        receipt = validate_pre_send(
            source,
            ir,
            plan,
            result,
            codec_registry=registry,
            codec_contract_version=codec.contract_version,
            paired_plans=plans,
            plan_set_profile=PlanSetProfile.G1_STRICT_MHR,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
        reversible = restore_original(result) == source
        target_only = (
            receipt.valid
            and "independent_target_only_diff" in receipt.checks
            and "non_history_projection_equal" in receipt.checks
        )
        if receipt.provider_invocation_allowed or not reversible or not target_only:
            _fail(
                "GPU_SMOKE_FIXTURE_RENDER_MISMATCH",
                "CPU codec pre-send/restore guard differs",
            )
        live_request = cast(
            dict[str, JsonValue],
            json.loads(canonical_json_bytes(result.rendered_request)),
        )
        live_request["model"] = cast(str, MODEL_IDENTITIES[model_id]["served_name"])
        diffs: list[JsonValue] = [
            {"kind": "TEXT", "value": item.to_dict()} for item in result.diffs
        ] + [{"kind": "LIST_INSERTION", "value": item.to_dict()} for item in result.list_insertions]
        mapping: dict[str, JsonValue] = {
            "source_mappings": [item.to_dict() for item in result.source_mappings],
            "plan_sha256": result.plan_sha256,
            "capability_sha256": result.capability_sha256,
            "validation_receipt_sha256": canonical_sha256(receipt.to_dict()),
            "target_only_diff": target_only,
            "source_mapping_reversible": reversible,
        }
        compiled[arm.value] = (live_request, diffs, mapping)
    return compiled


def compile_gpu_smoke_packet(
    qwen_fixture_path: str | os.PathLike[str],
    mai_fixture_path: str | os.PathLike[str],
    *,
    g1_5_seed: int = 1729,
) -> GpuSmokePacket:
    """Compile the 22 calls from the two frozen secret-free G1.5 fixtures."""

    if type(g1_5_seed) is not int or g1_5_seed != 1729:
        _fail("GPU_SMOKE_MATRIX_INVALID", "G1.5 seed must be exact 1729")
    fixtures = {
        "qwen3vl_8b": _read_frozen_fixture(qwen_fixture_path, "qwen3vl_8b"),
        "mai_ui_8b": _read_frozen_fixture(mai_fixture_path, "mai_ui_8b"),
    }
    calls: list[JsonValue] = []
    for model_id in MODEL_ORDER:
        tag = "qwen" if model_id == "qwen3vl_8b" else "mai"
        compiled = _compile_model_requests(model_id, fixtures[model_id])
        original_request = compiled["ORIGINAL"][0]
        for seed in REPLAY_SEEDS:
            for repeat in (1, 2):
                calls.append(
                    {
                        "call_id": f"g14-{tag}-s{seed}-r{repeat}",
                        "phase": "G1_4_CANARY",
                        "model_id": model_id,
                        "codec_id": None,
                        "seed": seed,
                        "repeat_index": repeat,
                        "arm": None,
                        "application_request": original_request,
                        "diff": None,
                        "mapping": None,
                        "render_evidence": None,
                    }
                )
        source_sha = canonical_sha256(original_request)
        for arm in G1_5_ARMS:
            request, diffs, mapping = compiled[arm]
            calls.append(
                {
                    "call_id": f"g15-{tag}-{arm.lower().replace('_', '-')}-s{g1_5_seed}",
                    "phase": "G1_5_CODEC",
                    "model_id": model_id,
                    "codec_id": MODEL_CODECS[model_id],
                    "seed": g1_5_seed,
                    "repeat_index": None,
                    "arm": arm,
                    "application_request": request,
                    "diff": diffs,
                    "mapping": mapping,
                    "render_evidence": {
                        "source_application_request_sha256": source_sha,
                        "rendered_application_request_sha256": canonical_sha256(request),
                        "diff_sha256": canonical_sha256(diffs),
                        "mapping_sha256": canonical_sha256(mapping),
                        "target_only_diff": cast(bool, mapping["target_only_diff"]),
                        "source_mapping_reversible": cast(
                            bool, mapping["source_mapping_reversible"]
                        ),
                        "provider_invocation_allowed": False,
                    },
                }
            )
    source_bindings: dict[str, JsonValue] = {
        "g1_5_cpu_publication_sha256": G1_5_CPU_PUBLICATION_SHA256,
        "compiler_contract": PACKET_COMPILER_CONTRACT,
        "fixtures": cast(JsonValue, _FIXTURES),
    }
    subject = {
        "source_bindings": source_bindings,
        "g1_5_seed": g1_5_seed,
        "calls_sha256": canonical_sha256(calls),
    }
    packet: dict[str, JsonValue] = {
        "schema_version": SMOKE_PACKET_SCHEMA_VERSION,
        "packet_id": f"g1gpu-{canonical_sha256(subject)[:24]}",
        "synthetic_non_case": True,
        "secret_free": True,
        "formal_capsule": False,
        "contains_real_task_data": False,
        "generated_action_execution_allowed": False,
        "source_bindings": source_bindings,
        "calls": calls,
    }
    canonical = canonical_json_bytes(packet)
    return _validate_packet(packet, canonical, g1_5_seed=g1_5_seed)


def write_gpu_smoke_packet(
    packet: GpuSmokePacket, output_root: str | os.PathLike[str]
) -> dict[str, JsonValue]:
    """Install a compiled packet once under its content address outside Git."""

    root = Path(_absolute_lexical(os.fspath(output_root), "$.output_root"))
    repository_root = Path(__file__).resolve().parents[4]
    if root == Path("/") or root == repository_root or repository_root in root.parents:
        _fail("GPU_SMOKE_OUTPUT_ROOT_INVALID", "packet output root must be outside Git")
    relative = PurePosixPath("objects") / "sha256" / packet.sha256[:2] / packet.sha256
    _EvidenceStore._install(root / Path(relative), packet.canonical_bytes)
    return {
        "relative_path": str(relative),
        "sha256": packet.sha256,
        "byte_count": len(packet.canonical_bytes),
        "media_type": "application/json",
        "packet_id": cast(str, packet.value["packet_id"]),
    }


def _validate_packet(
    value: dict[str, JsonValue], canonical: bytes, *, g1_5_seed: int
) -> GpuSmokePacket:
    packet = _closed(value, _PACKET_KEYS, "$")
    if not (
        packet.get("schema_version") == SMOKE_PACKET_SCHEMA_VERSION
        and type(packet.get("packet_id")) is str
        and _ID_RE.fullmatch(cast(str, packet["packet_id"])) is not None
        and packet.get("synthetic_non_case") is True
        and packet.get("secret_free") is True
        and packet.get("formal_capsule") is False
        and packet.get("contains_real_task_data") is False
        and packet.get("generated_action_execution_allowed") is False
    ):
        _fail(
            "GPU_SMOKE_PACKET_HEADER_INVALID",
            "packet is not a synthetic non-case inert-action packet",
        )
    source_bindings = _closed(
        packet.get("source_bindings"), _SOURCE_BINDING_KEYS, "$.source_bindings"
    )
    if not (
        source_bindings.get("g1_5_cpu_publication_sha256") == G1_5_CPU_PUBLICATION_SHA256
        and source_bindings.get("compiler_contract") == PACKET_COMPILER_CONTRACT
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "packet compiler/publication binding differs",
            "$.source_bindings",
        )
    fixture_bindings = _closed(
        source_bindings.get("fixtures"), set(MODEL_ORDER), "$.source_bindings.fixtures"
    )
    for model_id in MODEL_ORDER:
        fixture = _closed(
            fixture_bindings.get(model_id),
            _FIXTURE_BINDING_KEYS,
            f"$.source_bindings.fixtures.{model_id}",
        )
        if fixture != _FIXTURES[model_id]:
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "fixture binding differs from the frozen secret-free source",
                f"$.source_bindings.fixtures.{model_id}",
            )
    raw_calls = packet.get("calls")
    expected = _expected_matrix(g1_5_seed)
    if type(raw_calls) is not list or len(raw_calls) != len(expected):
        _fail("GPU_SMOKE_MATRIX_INVALID", "packet must contain exactly 22 ordered calls", "$.calls")
    calls = tuple(
        _validate_call(raw, index=index, expected=expected[index])
        for index, raw in enumerate(cast(list[JsonValue], raw_calls))
    )
    call_ids = [cast(str, call.value["call_id"]) for call in calls]
    if len(set(call_ids)) != len(call_ids):
        _fail("GPU_SMOKE_MATRIX_INVALID", "call IDs must be unique", "$.calls")
    for model_id in MODEL_ORDER:
        model_calls = [
            call for call in calls if call.model_id == model_id and call.phase == "G1_5_CODEC"
        ]
        original = next(call for call in model_calls if call.value["arm"] == "ORIGINAL")
        source_sha = _sha256(original.application_request_bytes)
        for call in model_calls:
            evidence = cast(dict[str, JsonValue], call.value["render_evidence"])
            if evidence.get("source_application_request_sha256") != source_sha:
                _fail(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "all model arms must bind the Original source request",
                    f"$.calls[{calls.index(call)}].render_evidence.source_application_request_sha256",
                )
    _scan_for_secrets(cast(JsonValue, packet))
    return GpuSmokePacket(packet, canonical, _sha256(canonical), calls)


def load_gpu_smoke_packet(
    path: str | os.PathLike[str], expected_sha256: str, *, g1_5_seed: int
) -> GpuSmokePacket:
    """Load and validate the exact synthetic 22-call packet."""

    if not _is_sha256(expected_sha256):
        _fail("GPU_SMOKE_PACKET_HASH_INVALID", "expected packet digest is invalid")
    value, data = _read_json_nofollow(path, maximum_bytes=32 * 1024 * 1024)
    if _sha256(data) != expected_sha256:
        _fail("GPU_SMOKE_PACKET_HASH_MISMATCH", "packet digest differs")
    return _validate_packet(value, data, g1_5_seed=g1_5_seed)


def _assert_authority_active(authority: GpuLiveAuthority) -> None:
    now = datetime.now(UTC)
    if not (
        _parse_utc(authority.value["issued_at_utc"], "$.issued_at_utc")
        <= now
        < _parse_utc(authority.value["expires_at_utc"], "$.expires_at_utc")
    ):
        _fail("GPU_SMOKE_AUTHORITY_EXPIRED", "authority is not active")


def _launch_shim_invocation_receipt(
    authority: GpuLiveAuthority,
    *,
    execution_started: bool,
) -> dict[str, JsonValue]:
    binding = authority.launch_shim
    argument = f"{LAUNCH_SHIM_TOKEN_PREFIX}:{LAUNCH_SHIM_AUTHORITY_PATH}:{authority.sha256}"
    command = render_gpu_live_smoke_tool_command(authority)
    tool_shell_binary = cast(dict[str, JsonValue], authority.tool_shell["resolved_binary"])
    tool_shell_invocation = cast(dict[str, JsonValue], authority.tool_shell["invocation"])
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-launch-invocation/v1",
        "path": binding["path"],
        "sha256": binding["sha256"],
        "byte_count": binding["byte_count"],
        "authority_path": LAUNCH_SHIM_AUTHORITY_PATH,
        "authority_sha256": authority.sha256,
        "argument_prefix": LAUNCH_SHIM_TOKEN_PREFIX,
        "shell_argument_sha256": _sha256(argument.encode("ascii")),
        "shell_argument_byte_count": len(argument.encode("ascii")),
        "tool_shell_path": TOOL_SHELL_PATH,
        "tool_shell_resolved_path": TOOL_SHELL_RESOLVED_PATH,
        "tool_shell_sha256": tool_shell_binary["sha256"],
        "shell_command_sha256": _sha256(command.encode("ascii")),
        "shell_command_byte_count": len(command.encode("ascii")),
        "shell_command_prefix_sha256": tool_shell_invocation["command_prefix_sha256"],
        "shell_command_prefix_byte_count": tool_shell_invocation["command_prefix_byte_count"],
        "command_grammar": tool_shell_invocation["command_grammar"],
        "argc": 3,
        "argv_prefix": [binding["path"], "-c"],
        "shell_option": "-c",
        "login": False,
        "tty": False,
        "tool_shell_parameter_honored": True,
        "direct_exec_formally_proven": False,
        "shell_script_used": False,
        "pre_gate_tool_shell_used": True,
        "tool_shell_inherits_ambient_environment": True,
        "ambient_loader_environment_policy_bound": True,
        "launch_shim_clears_environment": True,
        "execution_started": execution_started,
        "pre_gate_invocation_attestation_formally_proven": False,
        "pre_gate_dynamic_loader_closure_formally_proven": False,
    }


def render_gpu_live_smoke_tool_command(authority: GpuLiveAuthority) -> str:
    """Return the exact transient exec-tool command; never persist it as authority input."""

    return (
        f"exec {authority.launch_shim['path']} -c {LAUNCH_SHIM_TOKEN_PREFIX}:"
        f"{LAUNCH_SHIM_AUTHORITY_PATH}:{authority.sha256}"
    )


def prepare_gpu_live_smoke(
    authority: GpuLiveAuthority,
    packet: GpuSmokePacket,
    model_config_manifest_path: str | os.PathLike[str],
) -> dict[str, JsonValue]:
    """Purely validate and render the D-034 call/launch data; never execute it."""

    if authority.bindings["smoke_packet_sha256"] != packet.sha256:
        _fail("GPU_SMOKE_PACKET_AUTHORITY_MISMATCH", "packet is not bound by authority")
    namespace = authority.network_namespace
    allowed_identities = {
        (
            cast(int, namespace["host_owner_uid"]),
            cast(int, namespace["host_owner_gid"]),
        ),
        (
            cast(int, namespace["inside_owner_uid"]),
            cast(int, namespace["inside_owner_gid"]),
        ),
    }
    if (os.getuid(), os.getgid()) not in allowed_identities:
        _fail("GPU_SMOKE_AUTHORITY_OWNER_MISMATCH", "authority belongs to another host owner")
    _assert_authority_active(authority)
    receipt: LivePreparationReceipt = load_live_preparation(model_config_manifest_path)
    descriptors: list[OpenAIChatCallDescriptor] = []
    launches: list[VllmLaunchPlan] = []
    for model_id in MODEL_ORDER:
        model = cast(dict[str, JsonValue], authority.models[model_id])
        launches.append(
            prepare_vllm_launch_plan(receipt, model_id, cast(str, model["snapshot_path"]))
        )
    for call in packet.calls:
        descriptors.append(
            prepare_openai_chat_call(
                receipt,
                call.model_id,
                cast(dict[str, JsonValue], call.value["application_request"]),
                call.seed,
            )
        )
    descriptor_digest = _sha256(canonical_json_bytes([item.to_dict() for item in descriptors]))
    launch_digest = _sha256(canonical_json_bytes([item.to_dict() for item in launches]))
    return {
        "schema_version": PREPARATION_SCHEMA_VERSION,
        "decision_id": DECISION_ID,
        "authority_id": authority.authority_id,
        "authority_sha256": authority.sha256,
        "smoke_packet_sha256": packet.sha256,
        "live_preparation_receipt_sha256": receipt.sha256,
        "model_order": list(MODEL_ORDER),
        "call_count": len(descriptors),
        "g1_4_call_count": 12,
        "g1_5_call_count": 10,
        "call_descriptors_sha256": descriptor_digest,
        "launch_plans_sha256": launch_digest,
        "launch_shim": _launch_shim_invocation_receipt(
            authority,
            execution_started=False,
        ),
        "supplementary_groups_policy": cast(
            JsonValue,
            _validate_supplementary_group_authority(namespace.get("supplementary_groups")),
        ),
        "validated": True,
        "prepared": True,
        "execution_started": False,
        "client_created": False,
        "socket_opened": False,
        "subprocess_started": False,
        "gpu_probed": False,
        "gpu_used": False,
        "model_loaded": False,
        "provider_invoked": False,
        "generated_action_executed": False,
        "replay_executed": False,
        "provider_invocation_allowed": False,
        "execute_requires_explicit_entrypoint": True,
    }


def _hash_regular_file(path: str, *, expected_sha256: str | None = None) -> tuple[str, int]:
    """Hash one stable regular file, accepting a venv interpreter symlink target."""

    lexical = _absolute_lexical(path, "$.runtime.path")
    try:
        resolved = Path(lexical).resolve(strict=True)
        before = resolved.stat()
    except OSError as exc:
        raise GpuLiveSmokeError("GPU_SMOKE_ARTIFACT_UNREADABLE", "artifact is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        _fail("GPU_SMOKE_ARTIFACT_UNSAFE", "artifact is not a regular file")
    digest = hashlib.sha256()
    try:
        with resolved.open("rb", buffering=0) as handle:
            while True:
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = resolved.stat()
    except OSError as exc:
        raise GpuLiveSmokeError("GPU_SMOKE_ARTIFACT_UNREADABLE", "artifact read failed") from exc
    if _stat_identity(before) != _stat_identity(after):
        _fail("GPU_SMOKE_ARTIFACT_CHANGED", "artifact changed while hashing")
    actual = digest.hexdigest()
    if expected_sha256 is not None and actual != expected_sha256:
        _fail("GPU_SMOKE_ARTIFACT_HASH_MISMATCH", "artifact digest differs")
    return actual, before.st_size


def _hash_bound_executable(path: str, *, expected_sha256: str) -> tuple[str, int]:
    """Hash one authority-bound resolved executable without following links."""

    lexical = _absolute_lexical(path, "$.runtime.python_resolved_path")
    try:
        before = os.lstat(lexical)
        if not (
            stat.S_ISREG(before.st_mode)
            and before.st_mode & 0o111
            and os.path.realpath(lexical) == lexical
        ):
            _fail(
                "GPU_SMOKE_RUNTIME_INVALID",
                "bound executable must be an executable resolved regular file",
            )
        fd = _open_absolute_nofollow_file(lexical, os.O_RDONLY | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if _stat_identity(before) != _stat_identity(opened):
                _fail("GPU_SMOKE_RUNTIME_INVALID", "bound executable changed before open")
            digest = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                chunk = os.read(fd, min(8 * 1024 * 1024, remaining))
                if not chunk:
                    _fail("GPU_SMOKE_RUNTIME_INVALID", "bound executable was truncated")
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_RUNTIME_INVALID",
            "bound executable could not be hashed without following links",
        ) from exc
    actual = digest.hexdigest()
    if _stat_identity(opened) != _stat_identity(after) or actual != expected_sha256:
        _fail("GPU_SMOKE_RUNTIME_INVALID", "bound executable digest differs")
    return actual, opened.st_size


def _inspect_launch_shim_elf(
    path: str,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
    expected_mode: int = 0o500,
) -> dict[str, JsonValue]:
    """Parse and close the exact freestanding ELF trust boundary from pinned bytes."""

    lexical = _absolute_lexical(path, "$.launch_shim.path")
    try:
        before = os.lstat(lexical)
        if not (
            stat.S_ISREG(before.st_mode)
            and before.st_uid == expected_owner_uid
            and before.st_gid == expected_owner_gid
            and stat.S_IMODE(before.st_mode) == expected_mode
            and before.st_nlink == 1
            and os.path.realpath(lexical) == lexical
            and 0 < before.st_size <= 4 * 1024 * 1024
        ):
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim metadata is unsafe")
        fd = _open_absolute_nofollow_file(lexical, os.O_RDONLY | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if _tree_entry_identity(before) != _tree_entry_identity(opened):
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim changed before open")
            data = bytearray()
            remaining = opened.st_size
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim was truncated")
                data.extend(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        path_after = os.lstat(lexical)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim could not be inspected"
        ) from exc
    if not (
        _tree_entry_identity(opened)
        == _tree_entry_identity(after)
        == _tree_entry_identity(path_after)
    ):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim changed during inspection")
    payload = bytes(data)
    if len(payload) < 64:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim ELF header is truncated")
    try:
        (
            identity,
            elf_type,
            machine,
            version,
            entry_point,
            program_offset,
            section_offset,
            _flags,
            header_size,
            program_entry_size,
            program_count,
            section_entry_size,
            section_count,
            section_name_index,
        ) = struct.unpack_from("<16sHHIQQQIHHHHHH", payload, 0)
    except struct.error as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim ELF header is invalid"
        ) from exc
    if not (
        identity[:4] == b"\x7fELF"
        and identity[4] == 2
        and identity[5] == 1
        and identity[6] == 1
        and identity[7:] == b"\x00" * 9
        and elf_type == 2
        and machine == 62
        and version == 1
        and header_size == 64
        and program_entry_size == 56
        and 0 < program_count <= 64
        and (section_count == 0 or section_entry_size == 64)
        and (section_count == 0 or section_name_index < section_count)
    ):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim ELF identity differs")

    def checked_table_end(offset: int, count: int, entry_size: int) -> int:
        if offset < 0 or count < 0 or entry_size <= 0:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim ELF table is invalid")
        byte_count = count * entry_size
        end = offset + byte_count
        if byte_count > len(payload) or end < offset or end > len(payload):
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim ELF table overflows")
        return end

    checked_table_end(program_offset, program_count, program_entry_size)
    if section_count:
        checked_table_end(section_offset, section_count, section_entry_size)
    elif section_offset != 0:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim empty section table drifts")

    allowed_program_types = {0, 1, 4, 6, 0x6474E551, 0x6474E552, 0x6474E553}
    load_file_ranges: list[tuple[int, int]] = []
    load_memory_ranges: list[tuple[int, int]] = []
    executable_load_ranges: list[tuple[int, int]] = []
    gnu_stack_count = 0
    for index in range(program_count):
        offset = program_offset + index * program_entry_size
        try:
            (
                program_type,
                program_flags,
                file_offset,
                virtual_address,
                _physical_address,
                file_size,
                memory_size,
                alignment,
            ) = struct.unpack_from("<IIQQQQQQ", payload, offset)
        except struct.error as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim program header is truncated"
            ) from exc
        if program_type not in allowed_program_types or file_size > memory_size:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim program header is unknown")
        if alignment not in {0, 1} and alignment & (alignment - 1):
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim segment alignment is invalid")
        if alignment > 1 and file_offset % alignment != virtual_address % alignment:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim segment is misaligned")
        if file_size:
            end = file_offset + file_size
            if end > 0xFFFF_FFFF_FFFF_FFFF or end < file_offset or end > len(payload):
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim segment overflows")
            if program_type == 1:
                load_file_ranges.append((file_offset, end))
        if program_type == 1:
            memory_end = virtual_address + memory_size
            if memory_end > 0xFFFF_FFFF_FFFF_FFFF or memory_end < virtual_address:
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim memory segment overflows")
            load_memory_ranges.append((virtual_address, memory_end))
            if program_flags & 0x1:
                executable_load_ranges.append((virtual_address, memory_end))
        if program_type in {2, 3, 7}:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim has a forbidden loader segment")
        if program_type == 1 and program_flags & 0x2 and program_flags & 0x1:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim has a writable code segment")
        if program_type == 0x6474E551:
            gnu_stack_count += 1
            if program_flags & 0x1:
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim stack is executable")
    for previous, current in zip(
        sorted(load_file_ranges), sorted(load_file_ranges)[1:], strict=False
    ):
        if previous[1] > current[0]:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim load segments overlap")
    for previous, current in zip(
        sorted(load_memory_ranges), sorted(load_memory_ranges)[1:], strict=False
    ):
        if previous[1] > current[0]:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim memory segments overlap")
    if not (
        len(executable_load_ranges) == 1
        and executable_load_ranges[0][0] <= entry_point < executable_load_ranges[0][1]
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "launch shim entry point is outside its unique executable load segment",
        )
    if gnu_stack_count != 1:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim GNU_STACK is not unique")

    section_ranges: list[tuple[int, int]] = []
    allowed_section_types = {0, 1, 2, 3, 7, 8}
    for index in range(section_count):
        offset = section_offset + index * section_entry_size
        try:
            (
                _name,
                section_type,
                section_flags,
                _address,
                file_offset,
                section_size,
                _link,
                _info,
                _alignment,
                _entry_size,
            ) = struct.unpack_from("<IIQQQQIIQQ", payload, offset)
        except struct.error as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim section header is truncated"
            ) from exc
        if section_type not in allowed_section_types or section_type in {6, 14, 15, 16, 17}:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim section type is forbidden")
        if section_flags & 0x400:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim contains TLS")
        if section_flags & 0x1 and section_flags & 0x4:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim section is writable executable")
        if section_type != 8 and section_size:
            end = file_offset + section_size
            if end < file_offset or end > len(payload):
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim section overflows")
            section_ranges.append((file_offset, end))
    for previous, current in zip(sorted(section_ranges), sorted(section_ranges)[1:], strict=False):
        if previous[1] > current[0]:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "launch shim sections overlap")
    return {
        "elf_machine": "EM_X86_64",
        "elf_type": "ET_EXEC",
        "static": True,
        "pt_interp_allowed": False,
        "pt_dynamic_allowed": False,
        "dt_needed_allowed": False,
        "rpath_runpath_allowed": False,
        "init_array_allowed": False,
        "fini_array_allowed": False,
        "tls_segment_allowed": False,
        "writable_executable_segment_allowed": False,
        "executable_stack": False,
        "path": lexical,
        "resolved_path": lexical,
        "sha256": _sha256(payload),
        "byte_count": len(payload),
        "owner_uid": opened.st_uid,
        "owner_gid": opened.st_gid,
        "mode": stat.S_IMODE(opened.st_mode),
        "nlink": opened.st_nlink,
        "nofollow_revalidated": True,
        "parser_rejected_unknown_truncated_overlapping_or_overflowing_layout": True,
    }


def _inspect_tool_path_components(
    paths: tuple[str, ...],
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
) -> list[JsonValue]:
    result: list[JsonValue] = []
    for index, raw_path in enumerate(paths):
        path = _absolute_lexical(raw_path, f"$.tool_shell.component_bindings[{index}].path")
        try:
            metadata = os.lstat(path)
            resolved_path = os.path.realpath(path)
            if stat.S_ISLNK(metadata.st_mode):
                entry_type = "symlink"
                symlink_target: JsonValue = os.readlink(path)
                if stat.S_IMODE(metadata.st_mode) != 0o777 or metadata.st_nlink != 1:
                    _fail(
                        "GPU_SMOKE_SOURCE_BINDING_INVALID",
                        "tool-shell path symlink metadata differs",
                    )
            elif stat.S_ISDIR(metadata.st_mode):
                entry_type = "directory"
                symlink_target = None
                if stat.S_IMODE(metadata.st_mode) & 0o022:
                    _fail(
                        "GPU_SMOKE_SOURCE_BINDING_INVALID",
                        "tool-shell path directory is group/world writable",
                    )
            else:
                _fail(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "tool-shell component is neither directory nor symlink",
                )
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "tool-shell path component could not be inspected",
            ) from exc
        if metadata.st_uid != expected_owner_uid or metadata.st_gid != expected_owner_gid:
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "tool-shell path component owner differs",
            )
        result.append(
            {
                "path": path,
                "type": entry_type,
                "symlink_target": symlink_target,
                "resolved_path": resolved_path,
                "owner_uid": metadata.st_uid,
                "owner_gid": metadata.st_gid,
                "mode": stat.S_IMODE(metadata.st_mode),
                "nlink": metadata.st_nlink,
            }
        )
    return result


def _read_tool_shell_regular(
    path: str,
    *,
    resolved_path: str,
    expected_sha256: str,
    expected_byte_count: int,
    expected_owner_uid: int,
    expected_owner_gid: int,
    expected_mode: int,
) -> tuple[bytes, dict[str, JsonValue]]:
    lexical = _absolute_lexical(path, "$.tool_shell.binary.path")
    resolved = _absolute_lexical(resolved_path, "$.tool_shell.binary.resolved_path")
    try:
        if os.path.realpath(lexical) != resolved:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell path resolution differs")
        before = os.lstat(resolved)
        fd = _open_absolute_nofollow_file(resolved, os.O_RDONLY | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if _tree_entry_identity(before) != _tree_entry_identity(opened):
                _fail(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "tool-shell dependency changed before open",
                )
            payload = bytearray()
            remaining = opened.st_size
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    _fail(
                        "GPU_SMOKE_SOURCE_BINDING_INVALID",
                        "tool-shell dependency was truncated",
                    )
                payload.extend(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        path_after = os.lstat(resolved)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "tool-shell dependency could not be read",
        ) from exc
    data = bytes(payload)
    if not (
        _tree_entry_identity(opened)
        == _tree_entry_identity(after)
        == _tree_entry_identity(path_after)
        and stat.S_ISREG(opened.st_mode)
        and opened.st_uid == expected_owner_uid
        and opened.st_gid == expected_owner_gid
        and stat.S_IMODE(opened.st_mode) == expected_mode
        and opened.st_nlink == 1
        and opened.st_size == expected_byte_count
        and _sha256(data) == expected_sha256
    ):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell dependency binding differs")
    return data, {
        "path": lexical,
        "resolved_path": resolved,
        "sha256": expected_sha256,
        "byte_count": expected_byte_count,
        "owner_uid": opened.st_uid,
        "owner_gid": opened.st_gid,
        "mode": stat.S_IMODE(opened.st_mode),
        "nlink": opened.st_nlink,
    }


def _parse_dynamic_elf(payload: bytes) -> dict[str, JsonValue]:
    if len(payload) < 64:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF header is truncated")
    try:
        (
            identity,
            elf_type,
            machine,
            version,
            _entry,
            program_offset,
            _section_offset,
            _flags,
            header_size,
            program_entry_size,
            program_count,
            _section_entry_size,
            _section_count,
            _section_name_index,
        ) = struct.unpack_from("<16sHHIQQQIHHHHHH", payload, 0)
    except struct.error as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF header is invalid"
        ) from exc
    if not (
        identity[:4] == b"\x7fELF"
        and identity[4:7] == b"\x02\x01\x01"
        and identity[7] in {0, 3}
        and identity[8:] == b"\x00" * 8
        and elf_type == 3
        and machine == 62
        and version == 1
        and header_size == 64
        and program_entry_size == 56
        and 0 < program_count <= 64
        and program_offset + program_count * program_entry_size <= len(payload)
    ):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF identity differs")
    loads: list[tuple[int, int, int]] = []
    dynamic_range: tuple[int, int] | None = None
    interpreter: str | None = None
    gnu_stack_flags: int | None = None
    allowed_program_types = {0, 1, 2, 3, 4, 6, 7, 0x6474E550, 0x6474E551, 0x6474E552, 0x6474E553}
    for index in range(program_count):
        offset = program_offset + index * program_entry_size
        try:
            (
                program_type,
                program_flags,
                file_offset,
                virtual_address,
                _physical_address,
                file_size,
                memory_size,
                alignment,
            ) = struct.unpack_from("<IIQQQQQQ", payload, offset)
        except struct.error as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF program header is invalid"
            ) from exc
        if (
            program_type not in allowed_program_types
            or file_size > memory_size
            or file_offset + file_size < file_offset
            or file_offset + file_size > len(payload)
            or (alignment not in {0, 1} and alignment & (alignment - 1))
            or (alignment > 1 and file_offset % alignment != virtual_address % alignment)
        ):
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF segment is invalid")
        if program_type == 1:
            loads.append((virtual_address, file_offset, file_size))
        elif program_type == 2:
            if dynamic_range is not None or file_size == 0 or file_size % 16:
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF PT_DYNAMIC differs")
            dynamic_range = (file_offset, file_size)
        elif program_type == 3:
            if interpreter is not None or file_size < 2:
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF PT_INTERP differs")
            raw = payload[file_offset : file_offset + file_size]
            if raw[-1:] != b"\x00" or b"\x00" in raw[:-1]:
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF interpreter is invalid")
            try:
                interpreter = raw[:-1].decode("ascii")
            except UnicodeDecodeError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "dynamic ELF interpreter is not ASCII",
                ) from exc
        elif program_type == 0x6474E551:
            if gnu_stack_flags is not None:
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF GNU_STACK repeats")
            gnu_stack_flags = program_flags
    if dynamic_range is None or interpreter is None or gnu_stack_flags is None:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF loader boundary is incomplete")
    dynamic_offset, dynamic_size = dynamic_range
    dynamic_entries: list[tuple[int, int]] = []
    terminated = False
    for offset in range(dynamic_offset, dynamic_offset + dynamic_size, 16):
        tag, value = struct.unpack_from("<qQ", payload, offset)
        if tag == 0:
            terminated = True
            break
        dynamic_entries.append((tag, value))
    if not terminated:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF table is unterminated")
    string_table_values = [value for tag, value in dynamic_entries if tag == 5]
    string_size_values = [value for tag, value in dynamic_entries if tag == 10]
    if len(string_table_values) != 1 or len(string_size_values) != 1:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF string table differs")

    def virtual_to_file(virtual_address: int, byte_count: int) -> int:
        for load_virtual, load_file, load_size in loads:
            if (
                load_virtual <= virtual_address
                and virtual_address + byte_count >= virtual_address
                and virtual_address + byte_count <= load_virtual + load_size
            ):
                return load_file + virtual_address - load_virtual
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF string table is unmapped")

    string_size = string_size_values[0]
    string_offset = virtual_to_file(string_table_values[0], string_size)
    if string_offset + string_size > len(payload):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF string table overflows")
    string_table = payload[string_offset : string_offset + string_size]

    def dynamic_string(offset: int) -> str:
        if offset >= len(string_table):
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF string offset overflows")
        end = string_table.find(b"\x00", offset)
        if end < 0:
            _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF string is unterminated")
        try:
            return string_table[offset:end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF string is not ASCII"
            ) from exc

    needed = [dynamic_string(value) for tag, value in dynamic_entries if tag == 1]
    rpath_runpath = [dynamic_string(value) for tag, value in dynamic_entries if tag in {15, 29}]
    flags = [value for tag, value in dynamic_entries if tag == 30]
    flags_1 = [value for tag, value in dynamic_entries if tag == 0x6FFFFFFB]
    bind_now = (
        any(tag == 24 for tag, _value in dynamic_entries)
        or any(value & 0x8 for value in flags)
        or any(value & 0x1 for value in flags_1)
    )
    pie = any(value & 0x08000000 for value in flags_1)
    return {
        "machine": "EM_X86_64",
        "type": "ET_DYN",
        "elf_osabi": identity[7],
        "pt_interp": interpreter,
        "dt_needed": needed,
        "rpath_runpath_allowed": bool(rpath_runpath),
        "bind_now": bind_now,
        "pie": pie,
        "nx_stack": not bool(gnu_stack_flags & 0x1),
    }


def _inspect_dynamic_elf_header(payload: bytes) -> dict[str, JsonValue]:
    if len(payload) < 64:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF header is truncated")
    try:
        identity, elf_type, machine, version = struct.unpack_from("<16sHHI", payload, 0)
    except struct.error as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF header is invalid"
        ) from exc
    if not (
        identity[:4] == b"\x7fELF"
        and identity[4:7] == b"\x02\x01\x01"
        and identity[7] in {0, 3}
        and identity[8:] == b"\x00" * 8
        and elf_type == 3
        and machine == 62
        and version == 1
    ):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "dynamic ELF identity differs")
    return {"machine": "EM_X86_64", "type": "ET_DYN", "elf_osabi": identity[7]}


def _tool_shell_ambient_census(
    path: str,
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
) -> dict[str, JsonValue]:
    root = Path(_absolute_lexical(path, "$.tool_shell.loader_resolution.ambient_path"))
    resolved_root = root.resolve(strict=True)
    entries: list[dict[str, JsonValue]] = []
    forbidden: list[str] = []
    pending = [resolved_root]
    while pending:
        current = pending.pop()
        try:
            children = sorted(os.scandir(current), key=lambda item: item.name)
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "ambient loader path could not be enumerated",
            ) from exc
        for child in children:
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "ambient loader entry could not be inspected",
                ) from exc
            relative = str(Path(child.path).relative_to(resolved_root))
            if stat.S_ISDIR(metadata.st_mode):
                entry_type = "directory"
                if not (
                    metadata.st_uid == expected_owner_uid
                    and metadata.st_gid == expected_owner_gid
                    and not stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    _fail(
                        "GPU_SMOKE_SOURCE_BINDING_INVALID",
                        "ambient loader directory is not root-owned and read-only",
                    )
                pending.append(Path(child.path))
            elif stat.S_ISLNK(metadata.st_mode):
                entry_type = "symlink"
            elif stat.S_ISREG(metadata.st_mode):
                entry_type = "regular"
            else:
                entry_type = "other"
            entries.append(
                {
                    "path": relative,
                    "type": entry_type,
                    "size": metadata.st_size,
                    "directory_owner_role": ("HOST_ROOT" if entry_type == "directory" else None),
                    "directory_mode": (
                        stat.S_IMODE(metadata.st_mode) if entry_type == "directory" else None
                    ),
                    "directory_nlink": metadata.st_nlink if entry_type == "directory" else None,
                }
            )
            if child.name.startswith("libc.so") or child.name.startswith("ld-linux"):
                forbidden.append(relative)
            if len(entries) > 100_000:
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "ambient loader census is unbounded")
    entries.sort(key=lambda item: cast(str, item["path"]))
    forbidden.sort()
    return {
        "ambient_tree_entry_count": len(entries),
        "ambient_tree_entry_census_sha256": canonical_sha256(cast(JsonValue, entries)),
        "recursive_forbidden_soname_count": len(forbidden),
        "recursive_forbidden_soname_census_sha256": canonical_sha256(cast(JsonValue, forbidden)),
    }


def _inspect_tool_shell(
    *,
    expected_owner_uid: int,
    expected_owner_gid: int,
) -> dict[str, JsonValue]:
    dash_bytes, dash_binary = _read_tool_shell_regular(
        TOOL_SHELL_PATH,
        resolved_path=TOOL_SHELL_RESOLVED_PATH,
        expected_sha256=TOOL_SHELL_SHA256,
        expected_byte_count=TOOL_SHELL_BYTE_COUNT,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        expected_mode=0o755,
    )
    interpreter_bytes, interpreter_binary = _read_tool_shell_regular(
        TOOL_SHELL_PT_INTERP_PATH,
        resolved_path=TOOL_SHELL_PT_INTERP_RESOLVED_PATH,
        expected_sha256=TOOL_SHELL_PT_INTERP_SHA256,
        expected_byte_count=TOOL_SHELL_PT_INTERP_BYTE_COUNT,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        expected_mode=0o755,
    )
    libc_bytes, libc_binary = _read_tool_shell_regular(
        TOOL_SHELL_LIBC_PATH,
        resolved_path=TOOL_SHELL_LIBC_RESOLVED_PATH,
        expected_sha256=TOOL_SHELL_LIBC_SHA256,
        expected_byte_count=TOOL_SHELL_LIBC_BYTE_COUNT,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        expected_mode=0o755,
    )
    cache_bytes, cache_binary = _read_tool_shell_regular(
        TOOL_SHELL_LD_SO_CACHE_PATH,
        resolved_path=TOOL_SHELL_LD_SO_CACHE_PATH,
        expected_sha256=TOOL_SHELL_LD_SO_CACHE_SHA256,
        expected_byte_count=TOOL_SHELL_LD_SO_CACHE_BYTE_COUNT,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
        expected_mode=0o644,
    )
    dash_elf = _parse_dynamic_elf(dash_bytes)
    libc_elf = _parse_dynamic_elf(libc_bytes)
    interpreter_elf = _inspect_dynamic_elf_header(interpreter_bytes)
    if not (
        dash_elf
        == {
            "machine": "EM_X86_64",
            "type": "ET_DYN",
            "elf_osabi": 0,
            "pt_interp": TOOL_SHELL_PT_INTERP_PATH,
            "dt_needed": ["libc.so.6"],
            "rpath_runpath_allowed": False,
            "bind_now": True,
            "pie": True,
            "nx_stack": True,
        }
        and interpreter_elf == {"machine": "EM_X86_64", "type": "ET_DYN", "elf_osabi": 3}
        and libc_elf["pt_interp"] == TOOL_SHELL_PT_INTERP_PATH
        and libc_elf["elf_osabi"] == 3
        and libc_elf["dt_needed"] == ["ld-linux-x86-64.so.2"]
        and libc_elf["rpath_runpath_allowed"] is False
        and _sha256(interpreter_bytes) == TOOL_SHELL_PT_INTERP_SHA256
    ):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell ELF dependency graph differs")
    try:
        os.lstat(TOOL_SHELL_LD_SO_PRELOAD_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID", "ld.so.preload absence could not be proven"
        ) from exc
    else:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "ld.so.preload must be absent")
    ambient_census = _tool_shell_ambient_census(
        TOOL_SHELL_AMBIENT_LD_LIBRARY_PATH,
        expected_owner_uid=expected_owner_uid,
        expected_owner_gid=expected_owner_gid,
    )
    if ambient_census["recursive_forbidden_soname_count"] != 0:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "ambient loader path shadows libc/loader")
    cache_selected_path = b"/lib/x86_64-linux-gnu/libc.so.6\x00"
    if cache_bytes.count(cache_selected_path) != 1 or cache_bytes.count(b"libc.so.6\x00") != 1:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "ld.so.cache libc selection differs")
    command_prefix = (
        f"exec {LAUNCH_SHIM_PATH} -c {LAUNCH_SHIM_TOKEN_PREFIX}:{LAUNCH_SHIM_AUTHORITY_PATH}:"
    )
    return {
        "schema_version": TOOL_SHELL_SCHEMA_VERSION,
        "path_binding": {
            "lexical_path": TOOL_SHELL_PATH,
            "component_bindings": _inspect_tool_path_components(
                _TOOL_SHELL_PATH_COMPONENT_PATHS,
                expected_owner_uid=expected_owner_uid,
                expected_owner_gid=expected_owner_gid,
            ),
        },
        "resolved_binary": dash_binary,
        "elf": dash_elf,
        "interpreter_path_binding": {
            "lexical_path": TOOL_SHELL_PT_INTERP_PATH,
            "component_bindings": _inspect_tool_path_components(
                _TOOL_SHELL_INTERPRETER_COMPONENT_PATHS,
                expected_owner_uid=expected_owner_uid,
                expected_owner_gid=expected_owner_gid,
            ),
        },
        "interpreter_binary": interpreter_binary,
        "interpreter_elf": interpreter_elf,
        "dependencies": [
            {
                "soname": "libc.so.6",
                "binary": libc_binary,
                "elf_osabi": 3,
                "dt_needed": libc_elf["dt_needed"],
            }
        ],
        "ld_so_cache": cache_binary,
        "ld_so_preload": {"path": TOOL_SHELL_LD_SO_PRELOAD_PATH, "present": False},
        "loader_resolution": {
            "system_configuration_path_binding": {
                "lexical_path": "/etc",
                "component_bindings": _inspect_tool_path_components(
                    _TOOL_SHELL_SYSTEM_CONFIG_COMPONENT_PATHS,
                    expected_owner_uid=expected_owner_uid,
                    expected_owner_gid=expected_owner_gid,
                ),
            },
            "ambient_path_binding": {
                "lexical_path": TOOL_SHELL_AMBIENT_LD_LIBRARY_PATH,
                "component_bindings": _inspect_tool_path_components(
                    _TOOL_SHELL_AMBIENT_COMPONENT_PATHS,
                    expected_owner_uid=expected_owner_uid,
                    expected_owner_gid=expected_owner_gid,
                ),
            },
            **ambient_census,
            "ld_so_cache_selected_libc_path": TOOL_SHELL_LIBC_PATH,
            "selected_libc_unique": True,
        },
        "ambient_environment": {
            "required": {"LD_LIBRARY_PATH": TOOL_SHELL_AMBIENT_LD_LIBRARY_PATH},
            "forbidden_names": list(_TOOL_SHELL_FORBIDDEN_ENVIRONMENT_NAMES),
            "forbidden_prefixes": list(_TOOL_SHELL_FORBIDDEN_ENVIRONMENT_PREFIXES),
            "other_ld_environment_variables_allowed": False,
        },
        "invocation": {
            "shell_option": "-c",
            "login": False,
            "tty": False,
            "command_grammar": "EXEC_ABSOLUTE_SHIM_COLON_TOKEN_V2",
            "command_prefix": command_prefix,
            "command_prefix_sha256": _sha256(command_prefix.encode("ascii")),
            "command_prefix_byte_count": len(command_prefix.encode("ascii")),
            "command_authority_sha256_byte_count": 64,
            "command_total_byte_count": len(command_prefix.encode("ascii")) + 64,
        },
        "formal_claims": {
            "direct_exec_formally_proven": False,
            "pre_gate_dynamic_loader_closure_formally_proven": False,
        },
    }


def _normalize_tool_shell_owner_view(
    value: JsonValue,
    *,
    observed_uid: int,
    observed_gid: int,
) -> JsonValue:
    if isinstance(value, dict):
        normalized: dict[str, JsonValue] = {}
        for key, child in value.items():
            if key == "owner_uid" and child == observed_uid:
                normalized[key] = 0
            elif key == "owner_gid" and child == observed_gid:
                normalized[key] = 0
            else:
                normalized[key] = _normalize_tool_shell_owner_view(
                    child,
                    observed_uid=observed_uid,
                    observed_gid=observed_gid,
                )
        return normalized
    if isinstance(value, list):
        return [
            _normalize_tool_shell_owner_view(
                child,
                observed_uid=observed_uid,
                observed_gid=observed_gid,
            )
            for child in value
        ]
    return value


def _verify_tool_shell_runtime(authority: GpuLiveAuthority) -> dict[str, JsonValue]:
    namespace = authority.network_namespace
    observed_uid = cast(int, namespace["inside_unmapped_system_uid"])
    observed_gid = cast(int, namespace["inside_unmapped_system_gid"])
    observed = _inspect_tool_shell(
        expected_owner_uid=observed_uid,
        expected_owner_gid=observed_gid,
    )
    normalized = _normalize_tool_shell_owner_view(
        observed,
        observed_uid=observed_uid,
        observed_gid=observed_gid,
    )
    if normalized != cast(JsonValue, authority.tool_shell):
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "tool-shell runtime binding differs")
    loader = cast(dict[str, JsonValue], observed["loader_resolution"])
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-tool-shell-runtime/v1",
        "authority_binding_sha256": canonical_sha256(authority.tool_shell),
        "normalized_observed_binding_sha256": canonical_sha256(normalized),
        "owner_role": "HOST_ROOT_UNMAPPED_INSIDE_AUTHORIZED_USER_NAMESPACE",
        "authority_host_owner_uid": 0,
        "authority_host_owner_gid": 0,
        "observed_inside_owner_uid": observed_uid,
        "observed_inside_owner_gid": observed_gid,
        "owner_mapping_equivalent": True,
        "path_component_count": len(
            cast(
                list[JsonValue],
                cast(dict[str, JsonValue], observed["path_binding"])["component_bindings"],
            )
        ),
        "interpreter_component_count": len(
            cast(
                list[JsonValue],
                cast(dict[str, JsonValue], observed["interpreter_path_binding"])[
                    "component_bindings"
                ],
            )
        ),
        "system_configuration_component_count": len(
            cast(
                list[JsonValue],
                cast(
                    dict[str, JsonValue],
                    loader["system_configuration_path_binding"],
                )["component_bindings"],
            )
        ),
        "ambient_component_count": len(
            cast(
                list[JsonValue],
                cast(dict[str, JsonValue], loader["ambient_path_binding"])["component_bindings"],
            )
        ),
        "ambient_tree_entry_count": loader["ambient_tree_entry_count"],
        "ambient_tree_entry_census_sha256": loader["ambient_tree_entry_census_sha256"],
        "recursive_forbidden_soname_count": loader["recursive_forbidden_soname_count"],
        "ld_so_preload_absent": True,
        "authority_binding_match": True,
        "direct_exec_formally_proven": False,
        "pre_gate_dynamic_loader_closure_formally_proven": False,
    }


def _verify_launch_shim_runtime(authority: GpuLiveAuthority) -> dict[str, JsonValue]:
    binding = authority.launch_shim
    namespace = authority.network_namespace
    observed = _inspect_launch_shim_elf(
        cast(str, binding["path"]),
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
        expected_mode=cast(int, binding["mode"]),
    )
    compared_fields = (
        "path",
        "resolved_path",
        "sha256",
        "byte_count",
        "mode",
        "nlink",
        "elf_machine",
        "elf_type",
        "static",
        "pt_interp_allowed",
        "pt_dynamic_allowed",
        "dt_needed_allowed",
        "rpath_runpath_allowed",
        "init_array_allowed",
        "fini_array_allowed",
        "tls_segment_allowed",
        "writable_executable_segment_allowed",
        "executable_stack",
    )
    if not (
        all(observed[field] == binding[field] for field in compared_fields)
        and binding["owner_uid"] == namespace["host_owner_uid"]
        and binding["owner_gid"] == namespace["host_owner_gid"]
        and observed["owner_uid"] == namespace["inside_owner_uid"] == os.getuid()
        and observed["owner_gid"] == namespace["inside_owner_gid"] == os.getgid()
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "launch shim runtime bytes/ELF/owner mapping differ from authority",
        )
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-launch-shim-runtime/v1",
        "authority_binding_sha256": canonical_sha256(cast(JsonValue, binding)),
        "invocation": _launch_shim_invocation_receipt(
            authority,
            execution_started=True,
        ),
        "observed": observed,
        "authority_host_owner_uid": binding["owner_uid"],
        "authority_host_owner_gid": binding["owner_gid"],
        "observed_inside_owner_uid": observed["owner_uid"],
        "observed_inside_owner_gid": observed["owner_gid"],
        "owner_mapping_equivalent": True,
        "authority_binding_match": True,
        "pre_gate_invocation_attestation_formally_proven": False,
    }


def _nvidia_smi_binding(authority: GpuLiveAuthority) -> dict[str, JsonValue]:
    """Revalidate the exact nvidia-smi bytes immediately before each probe."""

    namespace = authority.network_namespace
    path = cast(str, namespace["nvidia_smi_path"])
    expected_sha256 = cast(str, namespace["nvidia_smi_sha256"])
    expected_byte_count = cast(int, namespace["nvidia_smi_byte_count"])
    digest, byte_count = _hash_bound_executable(path, expected_sha256=expected_sha256)
    if byte_count != expected_byte_count:
        _fail(
            "GPU_SMOKE_GPU_PROBE_BINDING_INVALID",
            "authority-bound nvidia-smi byte count differs",
        )
    return {
        "path": path,
        "sha256": digest,
        "byte_count": byte_count,
        "resolved_regular_executable": True,
        "nofollow_revalidated_immediately_before_probe": True,
    }


def _tree_entry_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _hash_open_regular_file(fd: int, metadata: os.stat_result) -> str:
    digest = hashlib.sha256()
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(fd, min(8 * 1024 * 1024, remaining))
        if not chunk:
            _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime tree file was truncated")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _enumerate_bound_runtime_tree(
    root: str,
    *,
    owner_uid: int,
    owner_gid: int,
    directory_mode: int,
    regular_mode: int,
    executable_mode: int,
    symlinks_allowed: bool,
    hardlinks_allowed: bool,
    recorded_owner_uid: int | None = None,
    recorded_owner_gid: int | None = None,
    forbid_bytecode_and_pth: bool = False,
    include_entries: bool = False,
) -> dict[str, JsonValue]:
    """Hash a closed runtime tree through no-follow dirfds and exact modes."""

    root_path = _absolute_lexical(root, "$.runtime_tree.root")
    if root_path == "/":
        _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime tree cannot be filesystem root")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        root_fd = os.open(root_path, directory_flags)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_RUNTIME_TREE_INVALID",
            "runtime tree root cannot be opened without following links",
        ) from exc
    entries: list[dict[str, JsonValue]] = []
    normalized_uid = owner_uid if recorded_owner_uid is None else recorded_owner_uid
    normalized_gid = owner_gid if recorded_owner_gid is None else recorded_owner_gid

    def validate_owner_mode(
        metadata: os.stat_result,
        *,
        expected_mode: int,
        label: str,
    ) -> None:
        if not (
            metadata.st_uid == owner_uid
            and metadata.st_gid == owner_gid
            and stat.S_IMODE(metadata.st_mode) == expected_mode
        ):
            _fail(
                "GPU_SMOKE_RUNTIME_TREE_INVALID",
                f"runtime {label} owner/group/mode differs from authority",
            )

    def walk(directory_fd: int, relative_directory: str, absolute_directory: str) -> None:
        directory_before = os.fstat(directory_fd)
        validate_owner_mode(
            directory_before,
            expected_mode=directory_mode,
            label="directory",
        )
        entries.append(
            {
                "path": relative_directory or ".",
                "entry_type": "DIRECTORY",
                "mode": stat.S_IMODE(directory_before.st_mode),
                "owner_role": "AUTHORIZED_OWNER",
                "byte_count": 0,
                "sha256": None,
                "symlink_target": None,
                "resolved_target": absolute_directory,
            }
        )
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_RUNTIME_TREE_INVALID",
                "runtime directory enumeration failed",
            ) from exc
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime entry name is unsafe")
            if forbid_bytecode_and_pth and (
                name == "__pycache__" or name.endswith((".pyc", ".pyo", ".pth"))
            ):
                _fail(
                    "GPU_SMOKE_RUNTIME_TREE_INVALID",
                    "sealed private runtime contains forbidden bytecode or pth startup code",
                )
            relative = f"{relative_directory}/{name}" if relative_directory else name
            absolute = f"{absolute_directory}/{name}"
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_RUNTIME_TREE_INVALID",
                    "runtime entry disappeared during enumeration",
                ) from exc
            if stat.S_ISDIR(before.st_mode):
                validate_owner_mode(before, expected_mode=directory_mode, label="directory")
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise GpuLiveSmokeError(
                        "GPU_SMOKE_RUNTIME_TREE_INVALID",
                        "runtime child directory could not be pinned",
                    ) from exc
                try:
                    if _tree_entry_identity(before) != _tree_entry_identity(os.fstat(child_fd)):
                        _fail(
                            "GPU_SMOKE_RUNTIME_TREE_INVALID",
                            "runtime directory changed before traversal",
                        )
                    walk(child_fd, relative, absolute)
                    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if _tree_entry_identity(before) != _tree_entry_identity(after):
                        _fail(
                            "GPU_SMOKE_RUNTIME_TREE_INVALID",
                            "runtime directory changed during traversal",
                        )
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISREG(before.st_mode):
                is_executable = bool(stat.S_IMODE(before.st_mode) & 0o111)
                validate_owner_mode(
                    before,
                    expected_mode=executable_mode if is_executable else regular_mode,
                    label="regular file",
                )
                if not hardlinks_allowed and before.st_nlink != 1:
                    _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime hardlink is forbidden")
                try:
                    file_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    raise GpuLiveSmokeError(
                        "GPU_SMOKE_RUNTIME_TREE_INVALID",
                        "runtime regular file could not be pinned",
                    ) from exc
                try:
                    opened = os.fstat(file_fd)
                    if _tree_entry_identity(before) != _tree_entry_identity(opened):
                        _fail(
                            "GPU_SMOKE_RUNTIME_TREE_INVALID",
                            "runtime file changed before hashing",
                        )
                    digest = _hash_open_regular_file(file_fd, opened)
                    after = os.fstat(file_fd)
                finally:
                    os.close(file_fd)
                if _tree_entry_identity(opened) != _tree_entry_identity(after):
                    _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime file changed while hashing")
                entries.append(
                    {
                        "path": relative,
                        "entry_type": "REGULAR_FILE",
                        "mode": stat.S_IMODE(before.st_mode),
                        "owner_role": "AUTHORIZED_OWNER",
                        "byte_count": before.st_size,
                        "sha256": digest,
                        "symlink_target": None,
                        "resolved_target": absolute,
                    }
                )
                continue
            if stat.S_ISLNK(before.st_mode):
                if not symlinks_allowed:
                    _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime symlink is forbidden")
                if before.st_uid != owner_uid or before.st_gid != owner_gid:
                    _fail(
                        "GPU_SMOKE_RUNTIME_TREE_INVALID",
                        "runtime symlink owner/group differs from authority",
                    )
                try:
                    target = os.readlink(name, dir_fd=directory_fd)
                    resolved = os.path.realpath(absolute)
                    resolved_metadata = os.stat(resolved, follow_symlinks=False)
                    if not stat.S_ISREG(resolved_metadata.st_mode):
                        _fail(
                            "GPU_SMOKE_RUNTIME_TREE_INVALID",
                            "runtime symlink does not resolve to a regular file",
                        )
                    resolved_is_executable = bool(stat.S_IMODE(resolved_metadata.st_mode) & 0o111)
                    validate_owner_mode(
                        resolved_metadata,
                        expected_mode=(executable_mode if resolved_is_executable else regular_mode),
                        label="symlink target",
                    )
                    resolved_fd = os.open(
                        resolved,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    )
                    try:
                        opened = os.fstat(resolved_fd)
                        if _tree_entry_identity(resolved_metadata) != _tree_entry_identity(opened):
                            _fail(
                                "GPU_SMOKE_RUNTIME_TREE_INVALID",
                                "runtime symlink target changed before hashing",
                            )
                        digest = _hash_open_regular_file(resolved_fd, opened)
                        resolved_after = os.fstat(resolved_fd)
                    finally:
                        os.close(resolved_fd)
                    after_target = os.readlink(name, dir_fd=directory_fd)
                    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise GpuLiveSmokeError(
                        "GPU_SMOKE_RUNTIME_TREE_INVALID",
                        "runtime symlink could not be pinned",
                    ) from exc
                if not (
                    target == after_target
                    and _tree_entry_identity(before) == _tree_entry_identity(after)
                    and _tree_entry_identity(opened) == _tree_entry_identity(resolved_after)
                ):
                    _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime symlink changed")
                entries.append(
                    {
                        "path": relative,
                        "entry_type": "SYMLINK",
                        "mode": stat.S_IMODE(before.st_mode),
                        "owner_role": "AUTHORIZED_OWNER",
                        "byte_count": opened.st_size,
                        "sha256": digest,
                        "symlink_target": target,
                        "resolved_target": resolved,
                    }
                )
                continue
            _fail(
                "GPU_SMOKE_RUNTIME_TREE_INVALID",
                "runtime tree contains a device, socket, FIFO, or other special entry",
            )
        directory_after = os.fstat(directory_fd)
        if _tree_entry_identity(directory_before) != _tree_entry_identity(directory_after):
            _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime directory changed during census")

    try:
        walk(root_fd, "", root_path)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda item: cast(str, item["path"]))
    result: dict[str, JsonValue] = {
        "root": root_path,
        "tree_sha256": canonical_sha256(entries),
        "tree_entry_count": len(entries),
        "tree_byte_count": sum(cast(int, item["byte_count"]) for item in entries),
        "owner_uid": normalized_uid,
        "owner_gid": normalized_gid,
        "aggregate_owner_identity": "AUTHORIZED_OWNER",
        "numeric_owner_excluded_from_tree_digest": True,
        "directory_mode": directory_mode,
        "regular_mode": regular_mode,
        "executable_mode": executable_mode,
        "symlinks_allowed": symlinks_allowed,
        "hardlinks_allowed": hardlinks_allowed,
        "forbidden_bytecode_or_pth_count": 0 if forbid_bytecode_and_pth else None,
        "all_entries_nofollow_revalidated": True,
    }
    if include_entries:
        result["entries"] = cast(JsonValue, entries)
    return result


def _assert_runtime_tree_matches_authority(
    inventory: dict[str, JsonValue],
    authority: dict[str, JsonValue],
    *,
    digest_key: str,
    count_key: str,
    bytes_key: str,
    label: str,
) -> None:
    if not (
        inventory["tree_sha256"] == authority[digest_key]
        and inventory["tree_entry_count"] == authority[count_key]
        and inventory["tree_byte_count"] == authority[bytes_key]
    ):
        _fail(
            "GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH",
            f"{label} complete tree differs from authority",
        )


def _verify_private_runtime_trees(
    authority: GpuLiveAuthority,
    *,
    phase: str,
) -> dict[str, JsonValue]:
    if phase not in {"PRE_IMPORT", "PRE_EXECUTION", "PASS_POSTFLIGHT", "FAIL_POSTFLIGHT"}:
        _fail("GPU_SMOKE_RUNTIME_TREE_INVALID", "runtime tree census phase is invalid")
    private = cast(dict[str, JsonValue], authority.value["private_runtime"])
    private_inventory = _enumerate_bound_runtime_tree(
        cast(str, private["root"]),
        owner_uid=cast(int, private["owner_uid"]),
        owner_gid=cast(int, private["owner_gid"]),
        directory_mode=cast(int, private["directory_mode"]),
        regular_mode=cast(int, private["regular_mode"]),
        executable_mode=cast(int, private["executable_mode"]),
        symlinks_allowed=cast(bool, private["symlinks_allowed"]),
        hardlinks_allowed=cast(bool, private["hardlinks_allowed"]),
        forbid_bytecode_and_pth=True,
    )
    _assert_runtime_tree_matches_authority(
        private_inventory,
        private,
        digest_key="tree_sha256",
        count_key="tree_entry_count",
        bytes_key="tree_byte_count",
        label="private Python runtime",
    )
    python_sha256, python_byte_count = _hash_bound_executable(
        cast(str, private["python_path"]),
        expected_sha256=cast(str, private["python_sha256"]),
    )
    if python_byte_count != private["python_byte_count"]:
        _fail(
            "GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH",
            "private Python executable byte count differs",
        )
    site_inventories: dict[str, JsonValue] = {}
    for role in ("client_runtime", "server_runtime"):
        runtime = cast(dict[str, JsonValue], authority.value[role])
        site_inventory = _enumerate_bound_runtime_tree(
            cast(str, runtime["site_packages_path"]),
            owner_uid=cast(int, runtime["site_packages_owner_uid"]),
            owner_gid=cast(int, runtime["site_packages_owner_gid"]),
            directory_mode=cast(int, runtime["site_packages_directory_mode"]),
            regular_mode=cast(int, runtime["site_packages_regular_mode"]),
            executable_mode=cast(int, runtime["site_packages_executable_mode"]),
            symlinks_allowed=cast(bool, runtime["site_packages_symlinks_allowed"]),
            hardlinks_allowed=cast(bool, runtime["site_packages_hardlinks_allowed"]),
            forbid_bytecode_and_pth=True,
        )
        _assert_runtime_tree_matches_authority(
            site_inventory,
            runtime,
            digest_key="site_packages_tree_sha256",
            count_key="site_packages_tree_entry_count",
            bytes_key="site_packages_tree_byte_count",
            label=role,
        )
        site_inventories[role] = site_inventory
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-runtime-tree-census/v1",
        "phase": phase,
        "private_runtime": private_inventory,
        "private_python": {
            "path": private["python_path"],
            "sha256": python_sha256,
            "byte_count": python_byte_count,
        },
        "site_packages": site_inventories,
        "owner_only_read_execute_modes": True,
        "authority_aggregate_match": True,
        "pre_post_digest_required": True,
        "same_owner_in_place_mutation_residual_disclosed": True,
        "native_dt_needed_dependency_closure_proven": False,
        "toctou_free_runtime_binding_proven": False,
        "formal_execution_closure_proven": False,
    }


_FICLONE = 0x40049409
_RENAMEAT2_SYSCALL_X86_64 = 316
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def _rename_noreplace(source: Path, destination: Path, *, error_code: str) -> None:
    if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
        _fail(
            error_code,
            "renameat2 no-replace is frozen only for Linux x86_64",
        )
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    result = cast(
        int,
        syscall(
            ctypes.c_long(_RENAMEAT2_SYSCALL_X86_64),
            ctypes.c_int(_AT_FDCWD),
            ctypes.c_char_p(os.fsencode(source)),
            ctypes.c_int(_AT_FDCWD),
            ctypes.c_char_p(os.fsencode(destination)),
            ctypes.c_uint(_RENAME_NOREPLACE),
        ),
    )
    if result != 0:
        observed_errno = ctypes.get_errno()
        raise GpuLiveSmokeError(
            error_code,
            f"renameat2 no-replace failed closed (errno={observed_errno})",
        )


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    _rename_noreplace(
        source,
        destination,
        error_code="GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
    )


def _copy_regular_file_by_reflink(
    source: Path,
    destination: Path,
    *,
    source_dir_fd: int | None = None,
    destination_dir_fd: int | None = None,
    expected_source_identity: tuple[int, ...] | None = None,
    allow_source_hardlinks: bool = False,
) -> dict[str, JsonValue]:
    """Clone one pinned regular file; never fall back to a byte-copy."""

    try:
        source_fd = os.open(
            source.name if source_dir_fd is not None else source,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=source_dir_fd,
        )
        try:
            source_before = os.fstat(source_fd)
            if not (
                stat.S_ISREG(source_before.st_mode)
                and source_before.st_nlink >= 1
                and (allow_source_hardlinks or source_before.st_nlink == 1)
                and (
                    expected_source_identity is None
                    or _tree_entry_identity(source_before) == expected_source_identity
                )
            ):
                _fail("GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED", "copy source is not regular")
            if source_before.st_size < 0:
                _fail("GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED", "copy source size is invalid")
            source_digest_before = _hash_open_regular_file(source_fd, source_before)
            os.lseek(source_fd, 0, os.SEEK_SET)
            destination_fd = os.open(
                destination.name if destination_dir_fd is not None else destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_dir_fd,
            )
            try:
                fcntl.ioctl(destination_fd, _FICLONE, source_fd)
                os.fsync(destination_fd)
                destination_metadata = os.fstat(destination_fd)
                expected_mode = 0o500 if stat.S_IMODE(source_before.st_mode) & 0o111 else 0o400
                os.fchmod(destination_fd, expected_mode)
                os.fsync(destination_fd)
                destination_after = os.fstat(destination_fd)
            finally:
                os.close(destination_fd)
            destination_read_fd = os.open(
                destination.name if destination_dir_fd is not None else destination,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=destination_dir_fd,
            )
            try:
                destination_reopened = os.fstat(destination_read_fd)
                destination_digest = _hash_open_regular_file(
                    destination_read_fd,
                    destination_reopened,
                )
                destination_reopened_after = os.fstat(destination_read_fd)
            finally:
                os.close(destination_read_fd)
            os.lseek(source_fd, 0, os.SEEK_SET)
            source_digest_after = _hash_open_regular_file(source_fd, source_before)
            source_after = os.fstat(source_fd)
        finally:
            os.close(source_fd)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "private runtime requires an exact same-filesystem FICLONE copy",
        ) from exc
    if not (
        _tree_entry_identity(source_before) == _tree_entry_identity(source_after)
        and source_digest_before == source_digest_after == destination_digest
        and stat.S_ISREG(destination_metadata.st_mode)
        and destination_metadata.st_size == source_before.st_size
        and destination_after.st_size == source_before.st_size
        and stat.S_IMODE(destination_after.st_mode) == expected_mode
        and destination_after.st_uid == os.getuid()
        and destination_after.st_gid == os.getgid()
        and destination_after.st_nlink == 1
        and (destination_after.st_dev, destination_after.st_ino)
        != (source_before.st_dev, source_before.st_ino)
        and _tree_entry_identity(destination_after)
        == _tree_entry_identity(destination_reopened)
        == _tree_entry_identity(destination_reopened_after)
    ):
        _fail(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "reflink source/destination identity or sealed mode differs",
        )
    return {
        "byte_count": source_before.st_size,
        "sha256": source_digest_before,
        "executable": bool(stat.S_IMODE(source_before.st_mode) & 0o111),
        "source_link_count": source_before.st_nlink,
        "source_hardlink_observed": source_before.st_nlink > 1,
        "source_hardlinks_accepted_for_copy_only": allow_source_hardlinks,
        "destination_link_count": destination_after.st_nlink,
        "source_destination_distinct_inodes": True,
        "source_pre_equals_source_post": True,
        "source_equals_destination": True,
    }


def _reflink_runtime_tree(
    source_root: Path,
    destination_root: Path,
    *,
    exclude_site_packages: bool,
    allow_source_hardlinks: bool,
) -> dict[str, JsonValue]:
    try:
        source_metadata = source_root.lstat()
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "private runtime copy source is unavailable",
        ) from exc
    if not stat.S_ISDIR(source_metadata.st_mode) or source_root.resolve(strict=True) != source_root:
        _fail(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "private runtime copy source root is linked or not a directory",
        )
    os.mkdir(destination_root, 0o700)
    source_fd = os.open(
        source_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    destination_fd = os.open(
        destination_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    content_entries: list[dict[str, JsonValue]] = []
    source_link_census: list[dict[str, JsonValue]] = []
    excluded_counts = {
        "__pycache__": 0,
        ".pyc": 0,
        ".pyo": 0,
        ".pth": 0,
        "stdlib/site-packages": 0,
    }

    def copy_directory(
        source_directory_fd: int,
        destination_directory_fd: int,
        relative_directory: str,
    ) -> None:
        source_before = os.fstat(source_directory_fd)
        if not stat.S_ISDIR(source_before.st_mode):
            _fail(
                "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                "pinned runtime source directory changed type",
            )
        try:
            names = sorted(os.listdir(source_directory_fd))
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                "private runtime source dirfd could not be enumerated",
            ) from exc
        content_entries.append(
            {
                "path": relative_directory or ".",
                "entry_type": "DIRECTORY",
                "byte_count": 0,
                "sha256": None,
                "executable": True,
            }
        )
        for name in names:
            excluded_category: str | None = None
            if name == "__pycache__":
                excluded_category = "__pycache__"
            elif name.endswith(".pyc"):
                excluded_category = ".pyc"
            elif name.endswith(".pyo"):
                excluded_category = ".pyo"
            elif name.endswith(".pth"):
                excluded_category = ".pth"
            elif exclude_site_packages and name == "site-packages":
                excluded_category = "stdlib/site-packages"
            if excluded_category is not None:
                excluded_counts[excluded_category] += 1
                continue
            try:
                before = os.stat(name, dir_fd=source_directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                    "private runtime source changed during dirfd traversal",
                ) from exc
            source_name = Path(name)
            destination_name = Path(name)
            relative = f"{relative_directory}/{name}" if relative_directory else name
            if stat.S_ISDIR(before.st_mode):
                os.mkdir(name, 0o700, dir_fd=destination_directory_fd)
                source_child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=source_directory_fd,
                )
                destination_child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=destination_directory_fd,
                )
                try:
                    if _tree_entry_identity(before) != _tree_entry_identity(
                        os.fstat(source_child_fd)
                    ):
                        _fail(
                            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                            "source directory changed before recursive copy",
                        )
                    copy_directory(source_child_fd, destination_child_fd, relative)
                    source_after = os.stat(
                        name,
                        dir_fd=source_directory_fd,
                        follow_symlinks=False,
                    )
                    if _tree_entry_identity(before) != _tree_entry_identity(source_after):
                        _fail(
                            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                            "source directory changed during recursive copy",
                        )
                    os.fsync(destination_child_fd)
                    os.fchmod(destination_child_fd, 0o500)
                    os.fsync(destination_child_fd)
                finally:
                    os.close(destination_child_fd)
                    os.close(source_child_fd)
            elif stat.S_ISREG(before.st_mode):
                if before.st_nlink < 1 or (not allow_source_hardlinks and before.st_nlink != 1):
                    _fail(
                        "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                        "source runtime regular file has a forbidden link count",
                    )
                copied = _copy_regular_file_by_reflink(
                    source_name,
                    destination_name,
                    source_dir_fd=source_directory_fd,
                    destination_dir_fd=destination_directory_fd,
                    expected_source_identity=_tree_entry_identity(before),
                    allow_source_hardlinks=allow_source_hardlinks,
                )
                source_link_census.append(
                    {
                        "path": relative,
                        "nlink": cast(int, copied["source_link_count"]),
                    }
                )
                content_entries.append(
                    {
                        "path": relative,
                        "entry_type": "REGULAR_FILE",
                        "byte_count": copied["byte_count"],
                        "sha256": copied["sha256"],
                        "executable": copied["executable"],
                    }
                )
                source_after = os.stat(
                    name,
                    dir_fd=source_directory_fd,
                    follow_symlinks=False,
                )
                if _tree_entry_identity(before) != _tree_entry_identity(source_after):
                    _fail(
                        "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                        "source regular file changed around reflink copy",
                    )
            else:
                _fail(
                    "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                    "private runtime source contains a symlink or special entry",
                )
        if _tree_entry_identity(source_before) != _tree_entry_identity(
            os.fstat(source_directory_fd)
        ):
            _fail(
                "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                "source directory changed during closed traversal",
            )

    try:
        if _tree_entry_identity(source_metadata) != _tree_entry_identity(os.fstat(source_fd)):
            _fail(
                "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                "source root changed before pinned traversal",
            )
        copy_directory(source_fd, destination_fd, "")
        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o500)
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
        os.close(source_fd)
    content_entries.sort(key=lambda item: cast(str, item["path"]))
    source_link_census.sort(key=lambda item: cast(str, item["path"]))
    content_sha256 = canonical_sha256(content_entries)
    source_hardlinked_entry_count = sum(cast(int, item["nlink"]) > 1 for item in source_link_census)
    source_max_nlink = max(
        (cast(int, item["nlink"]) for item in source_link_census),
        default=1,
    )
    return {
        "source_pre_content_sha256": content_sha256,
        "source_post_content_sha256": content_sha256,
        "destination_content_sha256": content_sha256,
        "content_entry_count": len(content_entries),
        "content_byte_count": sum(cast(int, item["byte_count"]) for item in content_entries),
        "excluded_entry_counts": excluded_counts,
        "source_hardlinked_entry_count": source_hardlinked_entry_count,
        "source_max_nlink": source_max_nlink,
        "source_link_census_sha256": canonical_sha256(source_link_census),
        "source_hardlinks_accepted_for_copy_only": allow_source_hardlinks,
        "destination_regular_files_single_linked": True,
        "source_pre_equals_source_post": True,
        "source_equals_destination": True,
        "source_traversal": "NOFOLLOW_RECURSIVE_DIRFD",
    }


def build_private_runtime(
    *,
    source_python_path: str,
    client_site_packages_path: str,
    server_site_packages_path: str,
    output_root: str,
) -> dict[str, JsonValue]:
    """Build and reopen a sealed owner-private Python 3.12 runtime via CoW clones."""

    source_python_requested = _absolute_lexical(
        source_python_path,
        "$.source_python_path",
    )
    try:
        source_python = Path(source_python_requested).resolve(strict=True)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "source Python could not be resolved",
        ) from exc
    source_root = source_python.parent.parent
    source_stdlib = source_root / "lib/python3.12"
    if source_python.name != "python3.12" or not source_stdlib.is_dir():
        _fail(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "source is not the frozen Python 3.12 base runtime shape",
        )
    output = Path(_absolute_lexical(output_root, "$.output_root"))
    if output == Path("/") or output.exists():
        _fail(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "private runtime output must be a new non-root directory",
        )
    parent = output.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "private runtime output parent is unavailable",
        ) from exc
    if not (
        stat.S_ISDIR(parent_metadata.st_mode)
        and parent.resolve(strict=True) == parent
        and parent_metadata.st_uid == os.getuid()
        and parent_metadata.st_gid == os.getgid()
        and stat.S_IMODE(parent_metadata.st_mode) & 0o022 == 0
    ):
        _fail(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            "private runtime output parent is linked, foreign-owned, or group/world-writable",
        )
    staging = parent / f".{output.name}.building-{os.getpid()}-{time.time_ns():x}"
    os.mkdir(staging, 0o700)
    try:
        os.mkdir(staging / "bin", 0o700)
        python_copy = _copy_regular_file_by_reflink(
            source_python,
            staging / "bin/python3.12",
        )
        os.mkdir(staging / "lib", 0o700)
        stdlib_copy = _reflink_runtime_tree(
            source_stdlib,
            staging / "lib/python3.12",
            exclude_site_packages=True,
            allow_source_hardlinks=True,
        )
        os.mkdir(staging / "site-packages", 0o700)
        client_site_copy = _reflink_runtime_tree(
            Path(
                _absolute_lexical(
                    client_site_packages_path,
                    "$.client_site_packages_path",
                )
            ),
            staging / "site-packages/client",
            exclude_site_packages=False,
            allow_source_hardlinks=False,
        )
        server_site_copy = _reflink_runtime_tree(
            Path(
                _absolute_lexical(
                    server_site_packages_path,
                    "$.server_site_packages_path",
                )
            ),
            staging / "site-packages/server",
            exclude_site_packages=False,
            allow_source_hardlinks=False,
        )
        for directory in (staging / "bin", staging / "lib", staging / "site-packages", staging):
            directory_fd = os.open(
                directory,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory_fd)
                os.fchmod(directory_fd, 0o500)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        _rename_directory_noreplace(staging, output)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException as exc:
        error = (
            exc
            if isinstance(exc, GpuLiveSmokeError)
            else GpuLiveSmokeError(
                "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
                f"private runtime build failed ({type(exc).__name__})",
            )
        )
        error.execution_detail = {
            "staging_path": str(staging),
            "output_installed": output.exists(),
            "automatic_recursive_deletion_performed": False,
        }
        if error is exc:
            raise error
        raise error from exc
    inventory = _enumerate_bound_runtime_tree(
        str(output),
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
    client_inventory = _enumerate_bound_runtime_tree(
        str(output / "site-packages/client"),
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
    server_inventory = _enumerate_bound_runtime_tree(
        str(output / "site-packages/server"),
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
    python_sha256, python_byte_count = _hash_bound_executable(
        str(output / "bin/python3.12"),
        expected_sha256=_hash_regular_file(str(output / "bin/python3.12"))[0],
    )
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-private-runtime-build/v1",
        "source_python_requested": source_python_requested,
        "source_python_resolved": str(source_python),
        "output_root": str(output),
        "python_path": str(output / "bin/python3.12"),
        "python_sha256": python_sha256,
        "python_byte_count": python_byte_count,
        "source_destination_content_bindings": {
            "python": python_copy,
            "stdlib": stdlib_copy,
            "client_site_packages": client_site_copy,
            "server_site_packages": server_site_copy,
        },
        "private_runtime_inventory": inventory,
        "client_site_packages_inventory": client_inventory,
        "server_site_packages_inventory": server_inventory,
        "copy_strategy": "LINUX_FICLONE_NO_FALLBACK",
        "excluded_names": [
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "*.pth",
            "stdlib/site-packages",
        ],
        "source_symlinks_allowed": False,
        "stdlib_source_hardlinks_allowed_for_copy_only": True,
        "python_and_site_source_hardlinks_allowed": False,
        "destination_symlinks_allowed": False,
        "destination_hardlinks_allowed": False,
        "destination_reopened_after_atomic_install": True,
        "automatic_recursive_deletion_performed": False,
        "gpu_probed": False,
        "network_opened": False,
        "model_loaded": False,
    }


def build_gpu_live_smoke_launch_shim(
    *,
    output_path: str = LAUNCH_SHIM_PATH,
) -> dict[str, JsonValue]:
    """Build, inspect, and atomically install the fixed CPU-only static launch shim."""

    output = Path(_absolute_lexical(output_path, "$.output_path"))
    if str(output) != LAUNCH_SHIM_PATH or output.exists():
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "launch shim output must be the absent frozen path",
        )
    repository_root = Path(__file__).resolve().parents[4]
    source = repository_root / _CRITICAL_SOURCE_FILES["launch_shim_source"]
    try:
        source_metadata = source.lstat()
        parent_metadata = output.parent.lstat()
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "launch shim source or output parent is unavailable",
        ) from exc
    if not (
        stat.S_ISREG(source_metadata.st_mode)
        and source.resolve(strict=True) == source
        and source_metadata.st_uid == os.getuid()
        and source_metadata.st_gid == os.getgid()
        and stat.S_IMODE(source_metadata.st_mode) == 0o400
        and source_metadata.st_nlink == 1
        and stat.S_ISDIR(parent_metadata.st_mode)
        and output.parent.resolve(strict=True) == output.parent
        and parent_metadata.st_uid == os.getuid()
        and parent_metadata.st_gid == os.getgid()
        and stat.S_IMODE(parent_metadata.st_mode) == 0o700
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "launch shim source/output parent is not owner-only and sealed",
        )
    source_expected_sha256 = _hash_regular_file(str(source))[0]
    source_sha256, source_byte_count = _hash_source_regular_file(
        source,
        expected_sha256=source_expected_sha256,
    )
    compiler_requested = Path(LAUNCH_SHIM_GCC_PATH)
    try:
        compiler_resolved = compiler_requested.resolve(strict=True)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "launch shim compiler is unavailable",
        ) from exc
    compiler_sha256, compiler_byte_count = _hash_regular_file(str(compiler_requested))
    staging = output.parent / (f".{output.name}.building-{os.getpid()}-{time.time_ns():x}")
    os.mkdir(staging, 0o700)
    candidate = staging / output.name
    compiler_log = staging / "compiler.log"
    compile_argv = [
        LAUNCH_SHIM_GCC_PATH,
        *LAUNCH_SHIM_BUILD_FLAGS,
        "-o",
        str(candidate),
        str(source),
    ]
    returncode: int | None = None
    interrupted: BaseException | None = None
    try:
        log_fd = os.open(
            compiler_log,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            process = subprocess.Popen(
                compile_argv,
                cwd=repository_root,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                close_fds=True,
            )
            try:
                returncode = process.wait()
            except BaseException as exc:
                interrupted = exc
                while process.poll() is None:
                    try:
                        process.wait()
                    except BaseException:
                        continue
                returncode = process.returncode
        finally:
            os.close(log_fd)
        if interrupted is not None:
            raise interrupted
        compiler_log_metadata = compiler_log.lstat()
        if not (
            stat.S_ISREG(compiler_log_metadata.st_mode)
            and compiler_log_metadata.st_uid == os.getuid()
            and compiler_log_metadata.st_gid == os.getgid()
            and stat.S_IMODE(compiler_log_metadata.st_mode) == 0o600
            and compiler_log_metadata.st_nlink == 1
            and compiler_log_metadata.st_size <= 1024 * 1024
        ):
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "launch shim compiler log exceeded its closed CPU-only bound",
            )
        compiler_log_sha256, compiler_log_byte_count = _hash_regular_file(str(compiler_log))
        if returncode != 0:
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "launch shim compiler returned non-zero; staging was preserved",
            )
        source_after = source.lstat()
        source_after_sha256, source_after_byte_count = _hash_source_regular_file(
            source,
            expected_sha256=source_sha256,
        )
        if not (
            _tree_entry_identity(source_metadata) == _tree_entry_identity(source_after)
            and source_after_sha256 == source_sha256
            and source_after_byte_count == source_byte_count
        ):
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "launch shim source changed during compilation",
            )
        candidate_fd = os.open(candidate, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            candidate_metadata = os.fstat(candidate_fd)
            if not (
                stat.S_ISREG(candidate_metadata.st_mode)
                and candidate_metadata.st_uid == os.getuid()
                and candidate_metadata.st_gid == os.getgid()
                and candidate_metadata.st_nlink == 1
            ):
                _fail(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "compiled launch shim is not an exact owned regular file",
                )
            os.fchmod(candidate_fd, 0o500)
            os.fsync(candidate_fd)
        finally:
            os.close(candidate_fd)
        _inspect_launch_shim_elf(
            str(candidate),
            expected_owner_uid=os.getuid(),
            expected_owner_gid=os.getgid(),
        )
        staging_fd = os.open(
            staging,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        compiler_log.unlink()
        _rename_noreplace(
            candidate,
            output,
            error_code="GPU_SMOKE_SOURCE_BINDING_INVALID",
        )
        parent_fd = os.open(
            output.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        staging.rmdir()
    except BaseException:
        # The exact owner-only staging directory is retained for audit; no broad deletion occurs.
        raise
    binary = _inspect_launch_shim_elf(
        str(output),
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
    )
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-launch-shim-build/v1",
        "source_path": str(source),
        "source_sha256": source_sha256,
        "source_byte_count": source_byte_count,
        "source_owner_uid": source_metadata.st_uid,
        "source_owner_gid": source_metadata.st_gid,
        "source_mode": stat.S_IMODE(source_metadata.st_mode),
        "source_nlink": source_metadata.st_nlink,
        "compiler_path": LAUNCH_SHIM_GCC_PATH,
        "compiler_resolved_path": str(compiler_resolved),
        "compiler_sha256": compiler_sha256,
        "compiler_byte_count": compiler_byte_count,
        "compile_argv": compile_argv,
        "compile_environment": {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
        "compiler_returncode": returncode,
        "compiler_log_sha256": compiler_log_sha256,
        "compiler_log_byte_count": compiler_log_byte_count,
        "output_path": str(output),
        "binary": binary,
        "installed_noreplace": True,
        "destination_reopened_after_install": True,
        "source_pre_equals_source_post": True,
        "source_to_binary_reproducibility_formally_proven": False,
        "static_elf_dependency_closure_proven": True,
        "staging_preserved_on_failure": True,
        "automatic_recursive_deletion_performed": False,
        "compiler_subprocess_started": True,
        "gpu_probed": False,
        "network_opened": False,
        "model_loaded": False,
    }


def _hash_source_regular_file(path: Path, *, expected_sha256: str) -> tuple[str, int]:
    """Hash one lexically exact source file without accepting a symlink."""

    lexical = Path(_absolute_lexical(str(path), "$.source.path"))
    try:
        before = lexical.lstat()
        if not stat.S_ISREG(before.st_mode) or lexical.resolve(strict=True) != lexical:
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "critical source path is linked or not a regular file",
            )
        fd = os.open(lexical, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if _stat_identity(before) != _stat_identity(opened):
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "critical source changed before open")
            digest = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "critical source truncated")
                digest.update(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "critical source could not be hashed without following links",
        ) from exc
    actual = digest.hexdigest()
    if _stat_identity(opened) != _stat_identity(after) or actual != expected_sha256:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "critical source digest differs")
    return actual, opened.st_size


def _enumerate_source_runtime_tree(source_root: str) -> dict[str, JsonValue]:
    root = Path(_absolute_lexical(source_root, "$.source.source_root"))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "source tree root could not be pinned",
        ) from exc
    entries: list[dict[str, JsonValue]] = []

    def walk(directory_fd: int, relative_directory: str) -> None:
        directory_before = os.fstat(directory_fd)
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "source tree dirfd could not be enumerated",
            ) from exc
        for name in names:
            if name == "__pycache__" or name.endswith((".pyc", ".pyo")):
                continue
            relative = f"{relative_directory}/{name}" if relative_directory else name
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "source tree member disappeared",
                ) from exc
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if _tree_entry_identity(before) != _tree_entry_identity(opened):
                        _fail(
                            "GPU_SMOKE_SOURCE_BINDING_INVALID",
                            "source directory changed before traversal",
                        )
                    entries.append({"path": relative, "entry_type": "DIRECTORY", "byte_count": 0})
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                _fail(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "source tree contains a symlink, hardlink, or special entry",
                )
            fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(fd)
                if _tree_entry_identity(before) != _tree_entry_identity(opened):
                    _fail(
                        "GPU_SMOKE_SOURCE_BINDING_INVALID",
                        "source file changed before hashing",
                    )
                digest = _hash_open_regular_file(fd, opened)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            if _tree_entry_identity(opened) != _tree_entry_identity(after):
                _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "source file changed while hashing")
            entries.append(
                {
                    "path": relative,
                    "entry_type": "REGULAR_FILE",
                    "byte_count": opened.st_size,
                    "sha256": digest,
                }
            )
        if _tree_entry_identity(directory_before) != _tree_entry_identity(os.fstat(directory_fd)):
            _fail(
                "GPU_SMOKE_SOURCE_BINDING_INVALID",
                "source directory changed during census",
            )

    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    entries.sort(key=lambda item: cast(str, item["path"]))
    return {
        "root": str(root),
        "tree_sha256": canonical_sha256(cast(JsonValue, entries)),
        "tree_entry_count": len(entries),
        "tree_byte_count": sum(cast(int, item["byte_count"]) for item in entries),
        "ignored_bytecode_cache_entries_not_importable": True,
        "symlink_count": 0,
        "hardlink_count": 0,
        "all_files_nofollow_revalidated": True,
    }


def _verify_runtime_bindings(authority: GpuLiveAuthority) -> dict[str, JsonValue]:
    runtime_tree_pre = _verify_private_runtime_trees(authority, phase="PRE_EXECUTION")
    source_tree_pre = _enumerate_source_runtime_tree(cast(str, authority.source["source_root"]))
    if not (
        source_tree_pre["tree_sha256"] == authority.source["source_tree_sha256"]
        and source_tree_pre["tree_entry_count"] == authority.source["source_tree_entry_count"]
        and source_tree_pre["tree_byte_count"] == authority.source["source_tree_byte_count"]
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "complete source tree differs from authority before execution",
        )
    client = cast(dict[str, JsonValue], authority.value["client_runtime"])
    server = cast(dict[str, JsonValue], authority.value["server_runtime"])
    repository_root = Path(cast(str, authority.source["worktree_root"]))
    source_root = Path(cast(str, authority.source["source_root"]))
    current_path = str(Path(sys.executable).absolute())
    authorized_client = cast(str, client["python_path"])
    if current_path != authorized_client or authorized_client != client["python_resolved_path"]:
        _fail(
            "GPU_SMOKE_CLIENT_RUNTIME_MISMATCH",
            "runner is not executing through the authority-bound resolved client interpreter",
        )
    client_sha, client_size = _hash_bound_executable(
        authorized_client, expected_sha256=cast(str, client["python_sha256"])
    )
    client_site_packages = Path(cast(str, client["site_packages_path"]))
    try:
        client_import_prefix = [str(Path(item).resolve(strict=True)) for item in sys.path[:2]]
        client_site_resolved = client_site_packages.resolve(strict=True)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_CLIENT_RUNTIME_MISMATCH",
            "client import bootstrap path is unavailable",
        ) from exc
    if not (
        client_site_resolved == client_site_packages
        and client_import_prefix == [str(source_root), str(client_site_packages)]
    ):
        _fail(
            "GPU_SMOKE_CLIENT_RUNTIME_MISMATCH",
            "client import search prefix differs from the authority-bound source/site paths",
        )
    if importlib.metadata.version("openai") != client["openai_version"]:
        _fail("GPU_SMOKE_CLIENT_RUNTIME_MISMATCH", "client OpenAI SDK version differs")
    server_path = cast(str, server["python_path"])
    if server_path != server["python_resolved_path"]:
        _fail(
            "GPU_SMOKE_SERVER_RUNTIME_MISMATCH",
            "server Python path is not its authority-bound resolved executable",
        )
    server_sha, server_size = _hash_bound_executable(
        server_path, expected_sha256=cast(str, server["python_sha256"])
    )
    server_site_packages = cast(str, server["site_packages_path"])
    try:
        if Path(server_site_packages).resolve(strict=True) != Path(server_site_packages):
            _fail(
                "GPU_SMOKE_SERVER_RUNTIME_MISMATCH",
                "server site-packages path is linked",
            )
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SERVER_RUNTIME_MISMATCH",
            "server site-packages path is unavailable",
        ) from exc
    server_site_literal = json.dumps(server_site_packages, ensure_ascii=True)
    probe_code = (
        "import json,sys;"
        f"sys.path.insert(0,{server_site_literal});"
        "import importlib.metadata;"
        "print(json.dumps({'packages':{k:importlib.metadata.version(k) for k in "
        "['openai','vllm','torch']},'isolated':sys.flags.isolated,"
        "'ignore_environment':sys.flags.ignore_environment,"
        "'no_site':sys.flags.no_site,"
        "'dont_write_bytecode':sys.flags.dont_write_bytecode,"
        "'pycache_prefix':sys.pycache_prefix,'site_module_loaded':'site' in sys.modules,"
        "'site_packages_path_present':sys.path[0]=="
        f"{server_site_literal}"
        "},sort_keys=True,separators=(',',':')))"
    )
    env = _server_environment(
        authority,
        str(Path(authority.runtime_scratch_root) / "namespace-launcher"),
    )
    completed = _run_owned_command(
        [
            server_path,
            *_ISOLATED_PYTHON_FLAGS,
            "-c",
            probe_code,
        ],
        env=env,
        timeout_seconds=30,
        error_code="GPU_SMOKE_SERVER_RUNTIME_PROBE_FAILED",
    )
    if completed.returncode != 0:
        _fail("GPU_SMOKE_SERVER_RUNTIME_PROBE_FAILED", "server runtime probe returned non-zero")
    try:
        versions = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SERVER_RUNTIME_PROBE_FAILED", "server runtime probe output is invalid"
        ) from exc
    expected_versions = {
        "openai": server["openai_version"],
        "vllm": server["vllm_version"],
        "torch": server["torch_version"],
    }
    if versions != {
        "packages": expected_versions,
        "isolated": 1,
        "ignore_environment": 1,
        "no_site": 1,
        "dont_write_bytecode": 1,
        "pycache_prefix": "/dev/null",
        "site_module_loaded": False,
        "site_packages_path_present": True,
    }:
        _fail("GPU_SMOKE_SERVER_RUNTIME_MISMATCH", "server package versions differ")
    if not (
        repository_root.resolve(strict=True) == repository_root
        and source_root.resolve(strict=True) == source_root
        and Path(__file__).resolve().parents[4] == repository_root
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "running module is outside the authority-bound lexical worktree",
        )
    mobile_world_package = importlib.import_module("mobile_world")
    package_paths = sorted(str(Path(item).resolve()) for item in mobile_world_package.__path__)
    if package_paths != [str(source_root / "mobile_world")]:
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "mobile_world import search path is not the exact worktree source root",
        )
    critical_bindings = cast(dict[str, JsonValue], authority.source["critical_files"])
    critical_receipts: list[JsonValue] = []
    for name, relative_path in _CRITICAL_SOURCE_FILES.items():
        binding = cast(dict[str, JsonValue], critical_bindings[name])
        expected_path = repository_root / relative_path
        if name in _CRITICAL_MODULE_NAMES:
            module = importlib.import_module(_CRITICAL_MODULE_NAMES[name])
            module_file = getattr(module, "__file__", None)
            module_cached = getattr(module, "__cached__", None)
            if type(module_file) is not str or Path(module_file).resolve() != expected_path:
                _fail(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "critical module import resolved outside the authority-bound worktree",
                )
            if type(module_cached) is not str or not module_cached.startswith("/dev/null/"):
                _fail(
                    "GPU_SMOKE_SOURCE_BINDING_INVALID",
                    "critical module bytecode cache is not disabled through /dev/null",
                )
        digest, byte_count = _hash_source_regular_file(
            expected_path,
            expected_sha256=cast(str, binding["sha256"]),
        )
        critical_receipts.append(
            {
                "name": name,
                "relative_path": relative_path,
                "sha256": digest,
                "byte_count": byte_count,
                "module_cached": (
                    getattr(
                        importlib.import_module(_CRITICAL_MODULE_NAMES[name]),
                        "__cached__",
                        None,
                    )
                    if name in _CRITICAL_MODULE_NAMES
                    else None
                ),
            }
        )
    module_sha = cast(
        str,
        cast(dict[str, JsonValue], critical_bindings["gpu_live_smoke"])["sha256"],
    )
    cli_sha = cast(
        str,
        cast(dict[str, JsonValue], critical_bindings["runner_cli"])["sha256"],
    )
    if not (
        module_sha == authority.bindings["runner_module_sha256"]
        and cli_sha == authority.bindings["runner_cli_sha256"]
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "duplicated runner/CLI digest bindings disagree",
        )
    git_path = cast(str, authority.source["git_path"])
    git_sha, git_size = _hash_regular_file(
        git_path,
        expected_sha256=cast(str, authority.source["git_sha256"]),
    )
    git_environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": cast(
            str,
            cast(dict[str, JsonValue], authority.network_namespace["launcher_environment"])["HOME"],
        ),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }
    revision_probe = _run_owned_command(
        [
            git_path,
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repository_root),
            "rev-parse",
            "HEAD",
        ],
        env=git_environment,
        timeout_seconds=10,
        error_code="GPU_SMOKE_SOURCE_BINDING_INVALID",
    )
    status_probe = _run_owned_command(
        [
            git_path,
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repository_root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        env=git_environment,
        timeout_seconds=30,
        error_code="GPU_SMOKE_SOURCE_BINDING_INVALID",
    )
    source_commit = revision_probe.stdout.decode("ascii", errors="strict").strip()
    if not (
        revision_probe.returncode == 0
        and source_commit == authority.bindings["source_git_commit"]
        and source_commit == authority.source["head_commit"]
        and status_probe.returncode == 0
        and status_probe.stdout == b""
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "source commit differs or worktree is not byte-clean",
        )
    pidfd_runtime = _verify_pidfd_runtime()
    return {
        "runtime_tree_pre": runtime_tree_pre,
        "client": {
            "python_path": authorized_client,
            "python_resolved_path": authorized_client,
            "python_sha256": client_sha,
            "python_byte_count": client_size,
            "site_packages_path": str(client_site_packages),
            "import_search_prefix": client_import_prefix,
            "openai_version": client["openai_version"],
        },
        "server": {
            "python_path": server_path,
            "python_resolved_path": server_path,
            "python_sha256": server_sha,
            "python_byte_count": server_size,
            "site_packages_path": server_site_packages,
            "packages": cast(JsonValue, versions["packages"]),
            "python_isolation": cast(
                JsonValue,
                {
                    key: versions[key]
                    for key in (
                        "isolated",
                        "ignore_environment",
                        "no_site",
                        "dont_write_bytecode",
                        "pycache_prefix",
                        "site_module_loaded",
                        "site_packages_path_present",
                    )
                },
            ),
        },
        "pidfd": pidfd_runtime,
        "owned_commands": [
            completed.receipt,
            revision_probe.receipt,
            status_probe.receipt,
        ],
        "implementation": {
            "runner_module_sha256": module_sha,
            "runner_cli_sha256": cli_sha,
            "source_git_commit": source_commit,
            "worktree_root": str(repository_root),
            "source_root": str(source_root),
            "git_path": git_path,
            "git_sha256": git_sha,
            "git_byte_count": git_size,
            "worktree_clean": True,
            "source_tree_pre": source_tree_pre,
            "critical_files": critical_receipts,
        },
    }


def _read_small_text(path: str, *, maximum_bytes: int = 64 * 1024) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "isolated namespace metadata is unreadable",
        ) from exc
    if len(data) > maximum_bytes:
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "isolated namespace metadata exceeds its closed bound",
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "isolated namespace metadata is not UTF-8",
        ) from exc


def _normalized_id_map(path: str) -> str:
    lines = [" ".join(line.split()) for line in _read_small_text(path).splitlines() if line.strip()]
    if len(lines) != 1:
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "namespace owner mapping is not one exact extent",
        )
    return lines[0]


def _initial_environment() -> dict[str, str]:
    try:
        raw = Path("/proc/self/environ").read_bytes()
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "initial runner environment is unreadable",
        ) from exc
    if len(raw) > 256 * 1024:
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "initial runner environment exceeds its closed bound",
        )
    result: dict[str, str] = {}
    for item in raw.rstrip(b"\0").split(b"\0") if raw else []:
        if b"=" not in item:
            _fail("GPU_SMOKE_NETWORK_NAMESPACE_INVALID", "runner environment is malformed")
        key_raw, value_raw = item.split(b"=", 1)
        try:
            key = key_raw.decode("ascii")
            value = value_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
                "runner environment contains non-canonical text",
            ) from exc
        if not key or key in result:
            _fail("GPU_SMOKE_NETWORK_NAMESPACE_INVALID", "runner environment is not unique")
        result[key] = value
    return result


def _runner_file_descriptor_census() -> dict[str, JsonValue]:
    """Prove the isolated runner retained only non-socket stdin/stdout/stderr."""

    try:
        observed = sorted(int(name) for name in os.listdir("/proc/self/fd") if name.isdecimal())
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED",
            "runner file-descriptor census is unavailable",
        ) from exc
    live: dict[int, os.stat_result] = {}
    for fd in observed:
        try:
            live[fd] = os.fstat(fd)
        except OSError:
            # /proc/self/fd contains the transient directory descriptor used
            # by listdir; it is closed before this revalidation.
            continue
    if set(live) != {0, 1, 2}:
        _fail(
            "GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED",
            "isolated runner retained a descriptor outside stdin/stdout/stderr",
        )
    descriptors: list[JsonValue] = []
    for fd in (0, 1, 2):
        metadata = live[fd]
        if stat.S_ISSOCK(metadata.st_mode):
            _fail(
                "GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED",
                "isolated runner standard descriptor is a socket",
            )
        descriptor_type = (
            "FIFO"
            if stat.S_ISFIFO(metadata.st_mode)
            else "CHARACTER_DEVICE"
            if stat.S_ISCHR(metadata.st_mode)
            else "REGULAR_FILE"
            if stat.S_ISREG(metadata.st_mode)
            else "OTHER_NON_SOCKET"
        )
        descriptors.append(
            {
                "fd": fd,
                "descriptor_type": descriptor_type,
                "socket": False,
                "inet_socket": False,
            }
        )
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-fd-census/v1",
        "open_fd_numbers": [0, 1, 2],
        "descriptors": descriptors,
        "open_fd_count": 3,
        "open_fd_count_above_stderr": 0,
        "standard_fd_socket_count": 0,
        "standard_fd_inet_socket_count": 0,
        "all_fds_above_stderr_closed": True,
        "standard_fds_non_inet": True,
        "foreign_process_fds_read": 0,
    }


def _retained_group_sensitive_fd_counts() -> tuple[int, int]:
    """Count this runner's KVM/Docker-path FDs and live AF_UNIX socket FDs."""

    filesystem_count = 0
    socket_inodes: set[str] = set()
    try:
        fd_names = sorted(os.listdir("/proc/self/fd"))
        for name in fd_names:
            if not name.isdecimal():
                continue
            try:
                target = os.readlink(f"/proc/self/fd/{name}")
            except FileNotFoundError:
                # The descriptor used by listdir can disappear before readlink.
                continue
            if target in {"/dev/kvm", "/run/docker.sock", "/var/run/docker.sock"}:
                filesystem_count += 1
            if target.startswith("socket:[") and target.endswith("]"):
                socket_inodes.add(target[8:-1])
        unix_rows = _read_small_text("/proc/net/unix").splitlines()[1:]
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "retained-group self-descriptor census is unavailable",
        ) from exc
    unix_inodes = {
        fields[6] for row in unix_rows if len(fields := row.split()) >= 7 and fields[6].isdecimal()
    }
    return filesystem_count, len(socket_inodes & unix_inodes)


def _loopback_self_connect_receipt() -> dict[str, JsonValue]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    accepted: socket.socket | None = None
    try:
        listener.settimeout(2.0)
        client.settimeout(2.0)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = cast(tuple[str, int], listener.getsockname())[1]
        client.connect(("127.0.0.1", port))
        accepted, peer = listener.accept()
        if peer[0] != "127.0.0.1":
            _fail("GPU_SMOKE_NETWORK_NAMESPACE_INVALID", "loopback peer is not loopback")
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "isolated loopback self-connect failed",
        ) from exc
    finally:
        if accepted is not None:
            accepted.close()
        client.close()
        listener.close()
    return {
        "address": "127.0.0.1",
        "protocol": "TCP",
        "self_connect_succeeded": True,
        "external_endpoint_contacted": False,
    }


def _supplementary_group_runtime_receipt(
    authority: GpuLiveAuthority,
    *,
    phase: str,
    status_fields: dict[str, str],
    file_descriptors: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    policy = _validate_supplementary_group_authority(
        authority.network_namespace.get("supplementary_groups")
    )
    try:
        status_groups = sorted(int(item) for item in status_fields.get("Groups", "").split())
    except ValueError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "runner supplementary groups are malformed",
        ) from exc
    observed_groups = sorted(os.getgroups())
    capability_fields = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    capabilities = {key: status_fields.get(key) for key in capability_fields}
    capabilities_zero = all(value == "0000000000000000" for value in capabilities.values())
    no_new_privs = status_fields.get("NoNewPrivs") == "1"
    setgroups_control = _read_small_text("/proc/self/setgroups").strip()
    filesystem_fd_count, unix_socket_fd_count = _retained_group_sensitive_fd_counts()
    if not (
        phase == "STAGE2_POST_SETPRIV"
        and observed_groups == _INSIDE_SUPPLEMENTARY_GIDS_SORTED
        and status_groups == _INSIDE_SUPPLEMENTARY_GIDS_SORTED
        and setgroups_control == "deny"
        and capabilities_zero
        and no_new_privs
        and file_descriptors.get("open_fd_count_above_stderr") == 0
        and file_descriptors.get("standard_fd_socket_count") == 0
        and filesystem_fd_count == 0
        and unix_socket_fd_count == 0
        and policy.get("docker_kvm_invocation_allowed") is False
    ):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "retained supplementary-group runtime boundary differs",
        )
    return {
        "schema_version": _SUPPLEMENTARY_GROUPS_RUNTIME_SCHEMA_VERSION,
        "phase": phase,
        "policy": _SUPPLEMENTARY_GROUP_POLICY,
        "owner_approved": True,
        "host_group_vector": _HOST_GROUP_VECTOR,
        "expected_inside_supplementary_gids_sorted": _INSIDE_SUPPLEMENTARY_GIDS_SORTED,
        "observed_inside_supplementary_gids_sorted": observed_groups,
        "proc_status_groups_sorted": status_groups,
        "inside_groups_empty_required": False,
        "setpriv_group_option": "--keep-groups",
        "setgroups_control": setgroups_control,
        "capability_drop_required_at_phase": True,
        "capability_sets": capabilities,
        "capability_sets_all_zero": capabilities_zero,
        "no_new_privs": no_new_privs,
        "docker_kvm_filesystem_fd_count": filesystem_fd_count,
        "docker_kvm_unix_socket_fd_count": unix_socket_fd_count,
        "docker_kvm_action_count": 0,
        "foreign_process_operation_count": 0,
        "docker_af_unix_capability_retained": True,
        "kvm_device_capability_retained": True,
        "docker_kvm_invocation_allowed": False,
        "docker_kvm_use_mechanically_proven_absent": False,
        "formal_supplementary_group_isolation_proven": False,
        "nonformal_residual_disclosed": True,
    }


def _verify_network_namespace(authority: GpuLiveAuthority) -> dict[str, JsonValue]:
    namespace = authority.network_namespace
    launch_shim_runtime = _verify_launch_shim_runtime(authority)
    tool_shell_runtime = _verify_tool_shell_runtime(authority)
    if not (
        sys.flags.isolated == 1
        and sys.flags.ignore_environment == 1
        and sys.flags.no_site == 1
        and sys.flags.dont_write_bytecode == 1
        and sys.pycache_prefix == namespace["python_pycache_prefix"] == "/dev/null"
        and "site" not in sys.modules
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "runner lacks exact -I -S -B -X pycache_prefix=/dev/null isolation",
        )
    file_descriptors = _runner_file_descriptor_census()
    expected_environment = cast(dict[str, JsonValue], namespace["launcher_environment"])
    current_environment = dict(os.environ)
    initial_environment = _initial_environment()
    if not (
        current_environment == expected_environment and initial_environment == expected_environment
    ):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "runner environment is not the exact authority-bound secret-free allowlist",
        )
    outer_fd_closure = _outer_fd_closure_receipt()
    outer_fd_closure_sha256 = canonical_sha256(outer_fd_closure)
    if not (
        outer_fd_closure_sha256 == namespace["outer_fd_closure_receipt_sha256"]
        and current_environment["GPU_SMOKE_OUTER_FD_CLOSURE_SHA256"] == outer_fd_closure_sha256
    ):
        _fail(
            "GPU_SMOKE_INHERITED_FD_CLOSURE_FAILED",
            "outer inherited-descriptor closure receipt differs from authority",
        )
    if not (
        os.getuid() == namespace["inside_owner_uid"] == authority.owner_uid
        and os.getgid() == namespace["inside_owner_gid"]
        and sorted(os.getgroups()) == _INSIDE_SUPPLEMENTARY_GIDS_SORTED
    ):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "runner identity is outside the authority-bound user namespace",
        )
    status_fields: dict[str, str] = {}
    for line in _read_small_text("/proc/self/status").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status_fields[key] = value.strip()
    capability_fields = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    try:
        status_groups = sorted(int(item) for item in status_fields.get("Groups", "").split())
    except ValueError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "runner supplementary groups are malformed",
        ) from exc
    if not (
        all(status_fields.get(key) == "0000000000000000" for key in capability_fields)
        and status_fields.get("NoNewPrivs") == "1"
        and status_groups == _INSIDE_SUPPLEMENTARY_GIDS_SORTED
    ):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "runner retained capabilities or lacks no-new-privileges",
        )
    uid_map = _normalized_id_map("/proc/self/uid_map")
    gid_map = _normalized_id_map("/proc/self/gid_map")
    if not (uid_map == namespace["uid_map_line"] and gid_map == namespace["gid_map_line"]):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "runner user namespace mapping differs from authority",
        )
    try:
        user_namespace_inode = os.readlink("/proc/self/ns/user")
        network_namespace_inode = os.readlink("/proc/self/ns/net")
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID", "namespace identities are unreadable"
        ) from exc
    interface_lines = _read_small_text("/proc/net/dev").splitlines()[2:]
    interfaces = sorted(line.split(":", 1)[0].strip() for line in interface_lines if ":" in line)
    if interfaces != namespace["expected_interfaces"]:
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "isolated network namespace contains a non-loopback interface",
        )
    ipv4_routes = [
        line for line in _read_small_text("/proc/net/route").splitlines()[1:] if line.strip()
    ]
    ipv4_route_interfaces = sorted(
        {fields[0] for line in ipv4_routes if len(fields := line.split()) >= 2}
    )
    ipv6_routes = [
        line for line in _read_small_text("/proc/net/ipv6_route").splitlines() if line.strip()
    ]
    ipv6_route_interfaces = sorted({fields[-1] for line in ipv6_routes if (fields := line.split())})
    ipv6_unreachable_defaults = 0
    ipv6_loopback_hosts = 0
    for line in ipv6_routes:
        fields = line.split()
        if len(fields) != 10 or fields[-1] != "lo":
            _fail(
                "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
                "isolated IPv6 route row is not the frozen loopback shape",
            )
        if (
            fields[0] == "0" * 32
            and fields[1] == "00"
            and fields[2] == "0" * 32
            and fields[3] == "00"
            and fields[4] == "0" * 32
            and fields[5].lower() == "ffffffff"
            and fields[8].lower() == "00200200"
        ):
            ipv6_unreachable_defaults += 1
        elif (
            fields[0] == "0" * 31 + "1"
            and fields[1].lower() == "80"
            and fields[2] == "0" * 32
            and fields[3] == "00"
            and fields[4] == "0" * 32
            and int(fields[5], 16) == 0
            and fields[8].lower() == "80200001"
        ):
            ipv6_loopback_hosts += 1
        else:
            _fail(
                "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
                "isolated IPv6 route is usable or differs from the frozen reject/loopback rows",
            )
    if (
        ipv4_routes
        or any(item != "lo" for item in ipv4_route_interfaces)
        or any(item != "lo" for item in ipv6_route_interfaces)
        or ipv6_unreachable_defaults != 2
        or ipv6_loopback_hosts != 1
    ):
        _fail(
            "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
            "isolated namespace contains a non-loopback or default route",
        )
    launcher_binaries: list[JsonValue] = []
    for prefix in ("env", "unshare", "ip", "setpriv"):
        path = cast(str, namespace[f"{prefix}_path"])
        digest, byte_count = _hash_regular_file(
            path,
            expected_sha256=cast(str, namespace[f"{prefix}_sha256"]),
        )
        launcher_binaries.append(
            {"role": prefix.upper(), "path": path, "sha256": digest, "byte_count": byte_count}
        )
    launcher_binaries.append(
        {
            "role": "NVIDIA_SMI",
            **_nvidia_smi_binding(authority),
        }
    )
    loopback_receipt = _loopback_self_connect_receipt()
    launcher_scratch = _scratch_census(
        Path(authority.runtime_scratch_root) / "namespace-launcher",
        run_id="NAMESPACE-LAUNCHER",
        model_id="LAUNCHER",
        phase="RUNNER_PREFLIGHT",
    )
    if not (
        launcher_scratch["entry_count"] == len(_SERVER_SCRATCH_DIRECTORY_NAMES)
        and launcher_scratch["regular_file_byte_count"] == 0
    ):
        _fail(
            "GPU_SMOKE_RUNTIME_SCRATCH_NOT_EMPTY",
            "namespace launcher scratch was not empty before live preflight",
        )
    supplementary_groups = _supplementary_group_runtime_receipt(
        authority,
        phase="STAGE2_POST_SETPRIV",
        status_fields=status_fields,
        file_descriptors=file_descriptors,
    )
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-network-namespace/v2",
        "implementation": namespace["implementation"],
        "host_owner_uid": namespace["host_owner_uid"],
        "host_owner_gid": namespace["host_owner_gid"],
        "inside_owner_uid": os.getuid(),
        "inside_owner_gid": os.getgid(),
        "inside_unmapped_system_uid": namespace["inside_unmapped_system_uid"],
        "inside_unmapped_system_gid": namespace["inside_unmapped_system_gid"],
        "supplementary_groups": supplementary_groups,
        "capability_sets": {key: status_fields[key] for key in capability_fields},
        "no_new_privs": True,
        "python_isolated": True,
        "python_ignore_environment": True,
        "python_no_site": True,
        "python_dont_write_bytecode": True,
        "python_pycache_prefix": sys.pycache_prefix,
        "site_module_loaded": False,
        "uid_map_line": uid_map,
        "gid_map_line": gid_map,
        "user_namespace_inode": user_namespace_inode,
        "network_namespace_inode": network_namespace_inode,
        "interfaces": interfaces,
        "ipv4_route_interfaces": ipv4_route_interfaces,
        "ipv6_route_interfaces": ipv6_route_interfaces,
        "default_route_count": 0,
        "ipv6_unreachable_default_sentinel_count": ipv6_unreachable_defaults,
        "ipv6_loopback_host_route_count": ipv6_loopback_hosts,
        "loopback": loopback_receipt,
        "pre_gate_launch_shim": launch_shim_runtime,
        "pre_gate_tool_shell": tool_shell_runtime,
        "launcher_binaries": launcher_binaries,
        "environment_keys": sorted(current_environment),
        "environment_sha256": canonical_sha256(cast(JsonValue, current_environment)),
        "launcher_scratch_pre": launcher_scratch,
        "outer_fd_closure": outer_fd_closure,
        "outer_fd_closure_sha256": outer_fd_closure_sha256,
        "inherited_file_descriptors": file_descriptors,
        "inet_inet6_external_network_mechanically_unavailable": True,
    }


def _required_snapshot_entries(
    receipt: LivePreparationReceipt, model_id: str
) -> dict[str, dict[str, JsonValue]]:
    binding = receipt.model(model_id)
    inventory = binding.checkpoint_inventory.files
    expected: dict[str, dict[str, JsonValue]] = {}
    for group in ("config_files", "weight_shards"):
        entries = inventory.get(group)
        if type(entries) is not list:
            _fail("GPU_SMOKE_SNAPSHOT_INVENTORY_INVALID", "frozen inventory is malformed")
        for raw in cast(list[JsonValue], entries):
            entry = cast(dict[str, JsonValue], raw)
            relative = cast(str, entry.get("path"))
            if relative in expected:
                _fail("GPU_SMOKE_SNAPSHOT_INVENTORY_INVALID", "snapshot member is duplicated")
            expected[relative] = {**entry, "group": group}
    tokenizer_entries = binding.tokenizer_binding.get("artifacts")
    if type(tokenizer_entries) is not list:
        _fail("GPU_SMOKE_SNAPSHOT_INVENTORY_INVALID", "tokenizer inventory is malformed")
    for raw in cast(list[JsonValue], tokenizer_entries):
        entry = cast(dict[str, JsonValue], raw)
        relative = cast(str, entry.get("path"))
        if relative in expected:
            _fail("GPU_SMOKE_SNAPSHOT_INVENTORY_INVALID", "snapshot member is duplicated")
        expected[relative] = {**entry, "group": "tokenizer_artifacts"}
    for relative in expected:
        pure = PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or str(pure) != relative
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            _fail("GPU_SMOKE_SNAPSHOT_INVENTORY_INVALID", "snapshot member is unsafe")
    return expected


def _enumerate_snapshot_tree(
    receipt: LivePreparationReceipt,
    model_id: str,
    snapshot_path: str,
) -> dict[str, JsonValue]:
    """Hash every entry in one resolved HF snapshot without following tree links."""

    snapshot = Path(_absolute_lexical(snapshot_path, "$.snapshot_path"))
    try:
        snapshot_lstat = snapshot.lstat()
        if not stat.S_ISDIR(snapshot_lstat.st_mode):
            _fail("GPU_SMOKE_SNAPSHOT_UNAVAILABLE", "local snapshot is not a directory")
        snapshot_resolved = snapshot.resolve(strict=True)
        blobs_root = (snapshot.parent.parent / "blobs").resolve(strict=True)
        if not blobs_root.is_dir():
            _fail("GPU_SMOKE_SNAPSHOT_UNAVAILABLE", "local HF blob directory is absent")
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SNAPSHOT_UNAVAILABLE", "local snapshot could not be inspected"
        ) from exc
    required = _required_snapshot_entries(receipt, model_id)
    observed: list[dict[str, JsonValue]] = []
    observed_files: set[str] = set()
    pending = [snapshot]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_SNAPSHOT_UNAVAILABLE", "snapshot tree enumeration failed"
            ) from exc
        for directory_entry in entries:
            member = Path(directory_entry.path)
            try:
                relative = member.relative_to(snapshot).as_posix()
                pure = PurePosixPath(relative)
                if str(pure) != relative or any(part in {"", ".", ".."} for part in pure.parts):
                    _fail("GPU_SMOKE_SNAPSHOT_TREE_MISMATCH", "snapshot path is unsafe")
                before = member.lstat()
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_SNAPSHOT_UNAVAILABLE", "snapshot member disappeared"
                ) from exc
            if stat.S_ISDIR(before.st_mode):
                observed.append(
                    {
                        "path": relative,
                        "entry_type": "DIRECTORY",
                        "symlink_target": None,
                        "resolved_target": str(member),
                        "byte_count": 0,
                        "sha256": None,
                        "group": "snapshot_directory",
                    }
                )
                pending.append(member)
                continue
            expected = required.get(relative)
            if stat.S_ISLNK(before.st_mode):
                try:
                    link_target = os.readlink(member)
                    resolved = member.resolve(strict=True)
                    resolved.relative_to(blobs_root)
                    if not resolved.is_file():
                        _fail("GPU_SMOKE_SNAPSHOT_LINK_UNSAFE", "snapshot link is not a file")
                    digest, size = _hash_regular_file(str(resolved))
                    after = member.lstat()
                except (OSError, ValueError) as exc:
                    raise GpuLiveSmokeError(
                        "GPU_SMOKE_SNAPSHOT_LINK_UNSAFE",
                        "snapshot link does not resolve inside its local HF blob store",
                    ) from exc
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
                    _fail("GPU_SMOKE_SNAPSHOT_CHANGED", "snapshot link changed while hashing")
                entry_type = "SYMLINK"
                resolved_target: JsonValue = str(resolved)
                symlink_target: JsonValue = link_target
            elif stat.S_ISREG(before.st_mode):
                if expected is None:
                    _fail(
                        "GPU_SMOKE_SNAPSHOT_LINK_UNSAFE",
                        "authority-extra snapshot files must be links into the local HF blob store",
                    )
                digest, size = _hash_regular_file(str(member))
                entry_type = "REGULAR_FILE"
                resolved_target = str(member)
                symlink_target = None
            else:
                _fail(
                    "GPU_SMOKE_SNAPSHOT_TREE_MISMATCH",
                    "snapshot contains a non-file, non-directory, non-link entry",
                )
            if expected is not None:
                if digest != expected.get("sha256") or size != expected.get("byte_count"):
                    _fail(
                        "GPU_SMOKE_SNAPSHOT_REQUIRED_MEMBER_MISMATCH",
                        "frozen checkpoint/tokenizer member differs",
                    )
                group = cast(str, expected["group"])
                observed_files.add(relative)
            else:
                group = "authority_extra"
            observed.append(
                {
                    "path": relative,
                    "entry_type": entry_type,
                    "symlink_target": symlink_target,
                    "resolved_target": resolved_target,
                    "byte_count": size,
                    "sha256": digest,
                    "group": group,
                }
            )
    missing = sorted(set(required) - observed_files)
    if missing:
        _fail(
            "GPU_SMOKE_SNAPSHOT_REQUIRED_MEMBER_MISSING",
            "snapshot omits a frozen checkpoint/tokenizer member",
        )
    observed.sort(key=lambda item: cast(str, item["path"]))
    aggregate = canonical_sha256(observed)
    total_bytes = sum(cast(int, item["byte_count"]) for item in observed)
    return {
        "snapshot_path": str(snapshot),
        "resolved_snapshot_path": str(snapshot_resolved),
        "entry_count": len(observed),
        "snapshot_tree_sha256": aggregate,
        "snapshot_tree_byte_count": total_bytes,
        "entries": cast(JsonValue, observed),
        "required_member_count": len(required),
        "additional_artifact_count": sum(item["group"] == "authority_extra" for item in observed),
        "hf_hub_offline": True,
        "local_files_only": True,
        "formal_model_immutability_proven": False,
        "toctou_free_model_binding_proven": False,
    }


def _verify_local_snapshots(
    authority: GpuLiveAuthority, receipt: LivePreparationReceipt
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for model_id in MODEL_ORDER:
        result[model_id] = _verify_local_snapshot(authority, receipt, model_id)
    return result


def _verify_local_snapshot(
    authority: GpuLiveAuthority,
    receipt: LivePreparationReceipt,
    model_id: str,
) -> dict[str, JsonValue]:
    if model_id not in MODEL_ORDER:
        _fail("GPU_SMOKE_MODEL_BINDING_INVALID", "snapshot model is not frozen")
    model_authority = cast(dict[str, JsonValue], authority.models[model_id])
    inventory = _enumerate_snapshot_tree(
        receipt,
        model_id,
        cast(str, model_authority["snapshot_path"]),
    )
    if not (
        model_authority.get("snapshot_tree_sha256") == inventory["snapshot_tree_sha256"]
        and model_authority.get("snapshot_tree_entry_count") == inventory["entry_count"]
        and model_authority.get("snapshot_tree_byte_count") == inventory["snapshot_tree_byte_count"]
    ):
        _fail(
            "GPU_SMOKE_SNAPSHOT_TREE_AUTHORITY_MISMATCH",
            "complete snapshot tree is not bound by authority",
        )
    return inventory


def inspect_authority_inputs(
    *,
    model_config_manifest_path: str | os.PathLike[str],
    launch_shim_path: str,
    smoke_packet_path: str,
    qwen_snapshot_path: str,
    mai_snapshot_path: str,
    client_python_path: str,
    server_python_path: str,
    client_site_packages_path: str,
    server_site_packages_path: str,
    outer_bootstrap_code_sha256: str,
    outer_bootstrap_code_byte_count: int,
) -> dict[str, JsonValue]:
    """Read and hash local authority inputs; never create a client/socket/GPU process."""

    if not (
        _is_sha256(outer_bootstrap_code_sha256)
        and type(outer_bootstrap_code_byte_count) is int
        and outer_bootstrap_code_byte_count > 0
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "outer stdlib bootstrap code binding is invalid",
        )
    manifest_path = _absolute_lexical(
        os.fspath(model_config_manifest_path),
        "$.model_config_manifest_path",
    )
    inspected_shim = _inspect_launch_shim_elf(
        launch_shim_path,
        expected_owner_uid=os.getuid(),
        expected_owner_gid=os.getgid(),
    )
    if inspected_shim["path"] != LAUNCH_SHIM_PATH:
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "launch shim inspection path differs from the frozen path",
        )
    inspected_tool_shell = _inspect_tool_shell(
        expected_owner_uid=0,
        expected_owner_gid=0,
    )
    packet_path = _absolute_lexical(smoke_packet_path, "$.smoke_packet_path")
    packet_value, packet_bytes = _read_json_nofollow(
        packet_path,
        maximum_bytes=32 * 1024 * 1024,
    )
    inspected_packet = _validate_packet(packet_value, packet_bytes, g1_5_seed=1729)
    expected_packet_path = (
        "/shared/linqiang/mobileworld_causal_replay_data/g1_gpu_smoke/"
        "d034-9845577c/packet-objects/objects/sha256/"
        f"{inspected_packet.sha256[:2]}/{inspected_packet.sha256}"
    )
    if packet_path != expected_packet_path:
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "smoke packet is outside its frozen content-addressed path",
        )
    receipt = load_live_preparation(model_config_manifest_path)
    snapshot_paths = {
        "qwen3vl_8b": _absolute_lexical(qwen_snapshot_path, "$.qwen_snapshot_path"),
        "mai_ui_8b": _absolute_lexical(mai_snapshot_path, "$.mai_snapshot_path"),
    }
    models: dict[str, JsonValue] = {}
    for model_id in MODEL_ORDER:
        inventory = _enumerate_snapshot_tree(receipt, model_id, snapshot_paths[model_id])
        models[model_id] = {
            "snapshot_path": snapshot_paths[model_id],
            "snapshot_tree_sha256": inventory["snapshot_tree_sha256"],
            "snapshot_tree_entry_count": inventory["entry_count"],
            "snapshot_tree_byte_count": inventory["snapshot_tree_byte_count"],
            "repository": MODEL_IDENTITIES[model_id]["repository"],
            "revision": MODEL_IDENTITIES[model_id]["revision"],
            "served_name": MODEL_IDENTITIES[model_id]["served_name"],
            "inventory": inventory,
        }
    requested_client_python = _absolute_lexical(client_python_path, "$.client_python_path")
    requested_server_python = _absolute_lexical(server_python_path, "$.server_python_path")
    try:
        resolved_client_python = str(Path(requested_client_python).resolve(strict=True))
        resolved_server_python = str(Path(requested_server_python).resolve(strict=True))
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_RUNTIME_INVALID", "runtime Python could not be resolved"
        ) from exc
    client_expected_sha = _hash_regular_file(resolved_client_python)[0]
    server_expected_sha = _hash_regular_file(resolved_server_python)[0]
    client_sha, client_bytes = _hash_bound_executable(
        resolved_client_python,
        expected_sha256=client_expected_sha,
    )
    server_sha, server_bytes = _hash_bound_executable(
        resolved_server_python,
        expected_sha256=server_expected_sha,
    )
    client_site_packages = _absolute_lexical(
        client_site_packages_path,
        "$.client_site_packages_path",
    )
    server_site_packages = _absolute_lexical(
        server_site_packages_path,
        "$.server_site_packages_path",
    )
    private_runtime_root = str(Path(resolved_client_python).parent.parent)
    if resolved_server_python != resolved_client_python:
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "client/server inspection must use the same sealed private Python executable",
        )
    private_runtime_inventory = _enumerate_bound_runtime_tree(
        private_runtime_root,
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
    )
    client_site_inventory = _enumerate_bound_runtime_tree(
        client_site_packages,
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
    )
    server_site_inventory = _enumerate_bound_runtime_tree(
        server_site_packages,
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
    )
    outer_python = "/usr/bin/python3.10"
    outer_python_sha256, outer_python_bytes = _hash_bound_executable(
        outer_python,
        expected_sha256=_hash_regular_file(outer_python)[0],
    )
    outer_stdlib_inventory = _enumerate_bound_runtime_tree(
        "/usr/lib/python3.10",
        owner_uid=0,
        owner_gid=0,
        directory_mode=0o755,
        regular_mode=0o644,
        executable_mode=0o755,
        symlinks_allowed=True,
        hardlinks_allowed=True,
    )
    client_versions = _read_distribution_versions(client_site_packages, ("openai",))
    server_versions = _read_distribution_versions(
        server_site_packages,
        ("openai", "vllm", "torch"),
    )
    repository_root = Path(__file__).resolve().parents[4]
    expected_manifest_path = str(
        repository_root / "mobileworld_audit_handoff/g1/model_config_manifest.v1.json"
    )
    if manifest_path != expected_manifest_path:
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "model manifest path differs from the sealed source binding",
        )
    implementation_paths = {
        "runner_module": Path(__file__).resolve(),
        "runner_cli": repository_root / "MobileWorld/scripts/run_g1_gpu_live_smoke.py",
        "env": Path("/usr/bin/env"),
        "unshare": Path("/usr/bin/unshare"),
        "ip": Path("/usr/bin/ip"),
        "setpriv": Path("/usr/bin/setpriv"),
        "nvidia_smi": Path("/usr/bin/nvidia-smi"),
        "git": Path("/usr/bin/git"),
    }
    implementation: dict[str, JsonValue] = {}
    for name, path in implementation_paths.items():
        digest, byte_count = _hash_regular_file(str(path))
        implementation[name] = {
            "path": str(path),
            "sha256": digest,
            "byte_count": byte_count,
        }
    revision_probe = _run_owned_command(
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repository_root),
            "rev-parse",
            "HEAD",
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
        },
        timeout_seconds=10,
        error_code="GPU_SMOKE_SOURCE_BINDING_INVALID",
    )
    try:
        source_commit = revision_probe.stdout.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SOURCE_BINDING_INVALID", "source commit could not be inspected"
        ) from exc
    if revision_probe.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        _fail("GPU_SMOKE_SOURCE_BINDING_INVALID", "source commit inspection failed")
    critical_source_files: dict[str, JsonValue] = {}
    critical_source_receipts: list[JsonValue] = []
    for name, relative_path in _CRITICAL_SOURCE_FILES.items():
        digest, byte_count = _hash_source_regular_file(
            repository_root / relative_path,
            expected_sha256=_hash_regular_file(str(repository_root / relative_path))[0],
        )
        critical_source_files[name] = {
            "relative_path": relative_path,
            "sha256": digest,
        }
        critical_source_receipts.append(
            {
                "name": name,
                "relative_path": relative_path,
                "sha256": digest,
                "byte_count": byte_count,
            }
        )
    source_tree_inventory = _enumerate_source_runtime_tree(str(repository_root / "MobileWorld/src"))
    bootstrap_subject: dict[str, JsonValue] = {
        "worktree_root": str(repository_root),
        "source_root": str(repository_root / "MobileWorld/src"),
        "client_site_packages_path": client_site_packages,
        "server_site_packages_path": server_site_packages,
        "python_flags": list(_ISOLATED_PYTHON_FLAGS),
        "python_pycache_prefix": "/dev/null",
        "server_bootstrap_code_sha256": _sha256(
            _server_bootstrap_code(server_site_packages).encode("utf-8")
        ),
        "critical_files": critical_source_files,
        "source_tree_sha256": source_tree_inventory["tree_sha256"],
        "source_tree_entry_count": source_tree_inventory["tree_entry_count"],
        "source_tree_byte_count": source_tree_inventory["tree_byte_count"],
        "outer_bootstrap_code_sha256": outer_bootstrap_code_sha256,
        "outer_bootstrap_code_byte_count": outer_bootstrap_code_byte_count,
    }
    bootstrap_manifest_sha256 = canonical_sha256(bootstrap_subject)
    launch_shim_elf_fields = {
        key: inspected_shim[key]
        for key in (
            "elf_machine",
            "elf_type",
            "static",
            "pt_interp_allowed",
            "pt_dynamic_allowed",
            "dt_needed_allowed",
            "rpath_runpath_allowed",
            "init_array_allowed",
            "fini_array_allowed",
            "tls_segment_allowed",
            "writable_executable_segment_allowed",
            "executable_stack",
        )
    }
    launch_shim: dict[str, JsonValue] = {
        "schema_version": LAUNCH_SHIM_SCHEMA_VERSION,
        "path": LAUNCH_SHIM_PATH,
        "resolved_path": LAUNCH_SHIM_PATH,
        "sha256": inspected_shim["sha256"],
        "byte_count": inspected_shim["byte_count"],
        "owner_uid": inspected_shim["owner_uid"],
        "owner_gid": inspected_shim["owner_gid"],
        "mode": inspected_shim["mode"],
        "nlink": inspected_shim["nlink"],
        "source_path": str(repository_root / _CRITICAL_SOURCE_FILES["launch_shim_source"]),
        "source_sha256": cast(
            str,
            cast(dict[str, JsonValue], critical_source_files["launch_shim_source"])["sha256"],
        ),
        "shell_option": "-c",
        "token_prefix": LAUNCH_SHIM_TOKEN_PREFIX,
        "runner_cli_path": str(repository_root / _CRITICAL_SOURCE_FILES["runner_cli"]),
        "smoke_packet_path": packet_path,
        "model_config_manifest_path": manifest_path,
        "bootstrap_sha256": outer_bootstrap_code_sha256,
        "bootstrap_byte_count": outer_bootstrap_code_byte_count,
        "confirmation": "EXECUTE-D034-SYNTHETIC-22-CALL-SMOKE",
        **launch_shim_elf_fields,
    }
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-authority-input-inspection/v2",
        "decision_id": DECISION_ID,
        "launch_shim": launch_shim,
        "launch_shim_inspection": inspected_shim,
        "tool_shell": inspected_tool_shell,
        "models": models,
        "client_runtime": {
            "input_python_path": requested_client_python,
            "python_path": resolved_client_python,
            "python_resolved_path": resolved_client_python,
            "python_sha256": client_sha,
            "python_byte_count": client_bytes,
            "site_packages_path": client_site_packages,
            "site_packages_inventory": client_site_inventory,
            "versions": client_versions,
            "versions_match_contract": client_versions == {"openai": "1.106.1"},
        },
        "server_runtime": {
            "input_python_path": requested_server_python,
            "python_path": resolved_server_python,
            "python_resolved_path": resolved_server_python,
            "python_sha256": server_sha,
            "python_byte_count": server_bytes,
            "site_packages_path": server_site_packages,
            "site_packages_inventory": server_site_inventory,
            "versions": server_versions,
            "versions_match_contract": server_versions
            == {"openai": "2.15.0", "torch": "2.8.0+cu126", "vllm": "0.11.0"},
        },
        "outer_runtime": {
            "python_path": outer_python,
            "python_resolved_path": outer_python,
            "python_sha256": outer_python_sha256,
            "python_byte_count": outer_python_bytes,
            "python_version": "3.10.12",
            "python_flags": list(_ISOLATED_PYTHON_FLAGS),
            "stdlib_root": "/usr/lib/python3.10",
            "stdlib_inventory": outer_stdlib_inventory,
        },
        "private_runtime": {
            "root": private_runtime_root,
            "python_path": resolved_client_python,
            "python_resolved_path": resolved_client_python,
            "python_sha256": client_sha,
            "python_byte_count": client_bytes,
            "python_version": "3.12.12",
            "python_flags": list(_ISOLATED_PYTHON_FLAGS),
            "stdlib_root": f"{private_runtime_root}/lib/python3.12",
            "inventory": private_runtime_inventory,
        },
        "live_preparation_receipt_sha256": receipt.sha256,
        "implementation": implementation,
        "source_git_commit": source_commit,
        "source": {
            "worktree_root": str(repository_root),
            "source_root": str(repository_root / "MobileWorld/src"),
            "git_path": "/usr/bin/git",
            "git_sha256": cast(str, cast(dict[str, JsonValue], implementation["git"])["sha256"]),
            "head_commit": source_commit,
            "critical_files": critical_source_files,
            "bootstrap_manifest_sha256": bootstrap_manifest_sha256,
            "source_tree_sha256": source_tree_inventory["tree_sha256"],
            "source_tree_entry_count": source_tree_inventory["tree_entry_count"],
            "source_tree_byte_count": source_tree_inventory["tree_byte_count"],
            "outer_bootstrap_code_sha256": outer_bootstrap_code_sha256,
            "outer_bootstrap_code_byte_count": outer_bootstrap_code_byte_count,
        },
        "source_inspection": {
            "critical_files": critical_source_receipts,
            "source_tree": source_tree_inventory,
            "bootstrap_manifest_subject": bootstrap_subject,
            "owned_command": revision_probe.receipt,
        },
        "gpu_probed": False,
        "client_created": False,
        "socket_opened": False,
        "subprocess_started": True,
        "model_loaded": False,
    }


def _runtime_site_packages_path(python_path: str) -> str:
    environment_root = Path(_absolute_lexical(python_path, "$.python_path")).parent.parent
    site_roots = sorted(environment_root.glob("lib/python*/site-packages"))
    if len(site_roots) != 1:
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            f"runtime has {len(site_roots)} site-packages directories",
        )
    site_root = site_roots[0]
    try:
        if not site_root.is_dir() or site_root.resolve(strict=True) != site_root:
            _fail(
                "GPU_SMOKE_RUNTIME_INVALID",
                "runtime site-packages must be an exact resolved directory",
            )
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_RUNTIME_INVALID", "runtime site-packages is unavailable"
        ) from exc
    return str(site_root)


def _read_distribution_versions(
    site_packages_path: str,
    names: tuple[str, ...],
) -> dict[str, JsonValue]:
    """Read another environment's dist-info metadata without executing it."""

    site_roots = [Path(_absolute_lexical(site_packages_path, "$.site_packages_path"))]
    result: dict[str, JsonValue] = {}
    for name in names:
        candidates: list[Path] = []
        for site_root in site_roots:
            candidates.extend(site_root.glob(f"{name.replace('-', '_')}-*.dist-info/METADATA"))
        if len(candidates) != 1:
            _fail(
                "GPU_SMOKE_RUNTIME_INVALID",
                f"runtime has {len(candidates)} {name} dist-info candidates",
            )
        metadata_path = candidates[0]
        try:
            fd = os.open(
                metadata_path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > 2 * 1024 * 1024:
                    _fail("GPU_SMOKE_RUNTIME_INVALID", "runtime metadata file is unsafe")
                data = _read_regular_fd(fd, before.st_size)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_RUNTIME_INVALID", "runtime metadata is unreadable"
            ) from exc
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
            _fail("GPU_SMOKE_RUNTIME_INVALID", "runtime metadata changed while read")
        version_lines = [
            line[9:].strip()
            for line in data.decode("utf-8").splitlines()
            if line.startswith("Version: ")
        ]
        if len(version_lines) != 1 or not version_lines[0]:
            _fail("GPU_SMOKE_RUNTIME_INVALID", "runtime metadata version is invalid")
        result[name] = version_lines[0]
    return result


def _inspect_gpu(authority: GpuLiveAuthority) -> dict[str, JsonValue]:
    if os.environ.get("CUDA_VISIBLE_DEVICES") != authority.gpu["uuid"]:
        _fail(
            "GPU_SMOKE_CUDA_VISIBLE_DEVICES_MISMATCH",
            "CUDA_VISIBLE_DEVICES must be the exact authority GPU UUID",
        )
    nvidia_smi = _nvidia_smi_binding(authority)
    executable = cast(str, nvidia_smi["path"])
    command = [
        executable,
        "--query-gpu=index,uuid,name,memory.total,memory.free",
        "--format=csv,noheader,nounits",
        "--id=" + cast(str, authority.gpu["uuid"]),
    ]
    completed = _run_owned_command(
        command,
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=15,
        error_code="GPU_SMOKE_GPU_PROBE_FAILED",
    )
    if completed.returncode != 0:
        _fail("GPU_SMOKE_GPU_PROBE_FAILED", "GPU probe returned non-zero")
    try:
        fields = [item.strip() for item in completed.stdout.decode("utf-8").strip().split(",")]
        index = int(fields[0])
        uuid = fields[1]
        name = fields[2]
        total = int(fields[3]) * 1024 * 1024
        free = int(fields[4]) * 1024 * 1024
    except (UnicodeDecodeError, ValueError, IndexError) as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_GPU_PROBE_FAILED", "GPU probe output is invalid"
        ) from exc
    if index != 0 or uuid != authority.gpu["uuid"] or not name.startswith("NVIDIA H200"):
        _fail("GPU_SMOKE_GPU_IDENTITY_MISMATCH", "probe does not identify authorized H200 GPU 0")
    minimum = cast(int, authority.gpu["minimum_free_memory_bytes"])
    if free < minimum:
        _fail(
            "GPU_SMOKE_GPU_CAPACITY_INSUFFICIENT", "shared GPU free memory is below authority floor"
        )
    return {
        "physical_index": index,
        "uuid": uuid,
        "name": name,
        "total_memory_bytes": total,
        "free_memory_bytes": free,
        "minimum_free_memory_bytes": minimum,
        "shared": True,
        "exclusive": False,
        "foreign_processes_signaled": 0,
        "nvidia_smi": nvidia_smi,
        "owned_command": completed.receipt,
    }


def _inspect_gpu_processes(
    authority: GpuLiveAuthority,
    *,
    owned_pids: set[int],
) -> dict[str, JsonValue]:
    """Read only PID/UID/memory accounting; never read a foreign cmdline or env."""

    nvidia_smi = _nvidia_smi_binding(authority)
    executable = cast(str, nvidia_smi["path"])
    command = [
        executable,
        f"--id={authority.gpu['uuid']}",
        "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
        "--format=csv,noheader,nounits",
    ]
    completed = _run_owned_command(
        command,
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=15,
        error_code="GPU_SMOKE_GPU_PROCESS_PROBE_FAILED",
    )
    if completed.returncode != 0:
        _fail("GPU_SMOKE_GPU_PROCESS_PROBE_FAILED", "GPU process accounting returned non-zero")
    rows: list[dict[str, JsonValue]] = []
    try:
        text = completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_GPU_PROCESS_PROBE_FAILED", "GPU process accounting is not UTF-8"
        ) from exc
    for line in text.splitlines() if text else ():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) != 3:
            _fail("GPU_SMOKE_GPU_PROCESS_PROBE_FAILED", "GPU process row is malformed")
        gpu_uuid, raw_pid, raw_memory = fields
        if gpu_uuid != authority.gpu["uuid"]:
            continue
        try:
            pid = int(raw_pid)
            memory_bytes = int(raw_memory) * 1024 * 1024
            if pid <= 1 or memory_bytes < 0:
                raise ValueError("unsafe process accounting")
            proc = Path("/proc") / str(pid)
            # Only UID and /proc stat start-time are captured.  No foreign
            # command line, executable, environment, fd, cwd, or /proc memory is read.
            uid_before = proc.stat().st_uid
            stat_text_before = (proc / "stat").read_text(encoding="utf-8")
            close_before = stat_text_before.rfind(")")
            stat_fields_before = stat_text_before[close_before + 2 :].split()
            starttime_ticks_before = int(stat_fields_before[19])
            stat_text_after = (proc / "stat").read_text(encoding="utf-8")
            close_after = stat_text_after.rfind(")")
            stat_fields_after = stat_text_after[close_after + 2 :].split()
            starttime_ticks_after = int(stat_fields_after[19])
            uid_after = proc.stat().st_uid
            if not (
                uid_before == uid_after
                and starttime_ticks_before == starttime_ticks_after
                and stat_text_before == stat_text_after
            ):
                raise ValueError("GPU process identity changed during metadata reads")
            uid = uid_before
            starttime_ticks = starttime_ticks_before
        except (OSError, ValueError, IndexError) as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_GPU_PROCESS_PROBE_FAILED",
                "GPU process PID/UID changed during accounting",
            ) from exc
        rows.append(
            {
                "pid": pid,
                "uid": uid,
                "starttime_ticks": starttime_ticks,
                "used_gpu_memory_bytes": memory_bytes,
                "classification": "OWN_LAUNCH" if pid in owned_pids else "BASELINE_OR_FOREIGN",
            }
        )
    rows.sort(key=lambda item: cast(int, item["pid"]))
    _nvidia_smi_binding(authority)
    repeated = _run_owned_command(
        command,
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=15,
        error_code="GPU_SMOKE_GPU_PROCESS_PROBE_FAILED",
    )
    if repeated.returncode != 0 or repeated.stdout != completed.stdout:
        _fail(
            "GPU_SMOKE_GPU_PROCESS_PROBE_FAILED",
            "GPU allocation rows changed during the pinned UID/starttime census",
        )
    return {
        "gpu_uuid": authority.gpu["uuid"],
        "processes": cast(JsonValue, rows),
        "process_count": len(rows),
        "foreign_cmdlines_read": 0,
        "foreign_environments_read": 0,
        "signals_sent": 0,
        "uid_before_after_match": True,
        "starttime_double_read_match": True,
        "allocation_rows_requeried_exactly": True,
        "command": command,
        "nvidia_smi": nvidia_smi,
        "owned_commands": [completed.receipt, repeated.receipt],
    }


def _gpu_process_identities(
    snapshot: dict[str, JsonValue],
) -> set[tuple[int, int, int]]:
    rows = cast(list[dict[str, JsonValue]], snapshot["processes"])
    return {
        (
            cast(int, row["pid"]),
            cast(int, row["uid"]),
            cast(int, row["starttime_ticks"]),
        )
        for row in rows
    }


def _assert_gpu_service_isolation(
    baseline: dict[str, JsonValue],
    current: dict[str, JsonValue],
    *,
    owned_pids: set[int],
    require_owned_absent: bool,
) -> dict[str, JsonValue]:
    baseline_ids = _gpu_process_identities(baseline)
    current_rows = cast(list[dict[str, JsonValue]], current["processes"])
    current_ids = _gpu_process_identities(current)
    owned_present = sorted(
        cast(int, row["pid"]) for row in current_rows if cast(int, row["pid"]) in owned_pids
    )
    owned_identities = {identity for identity in current_ids if identity[0] in owned_pids}
    unexpected = sorted(current_ids - baseline_ids - owned_identities)
    missing_baseline = sorted(baseline_ids - current_ids)
    if unexpected or missing_baseline:
        _fail(
            "GPU_SMOKE_FOREIGN_PROCESS_INVARIANCE_UNPROVEN",
            "shared-GPU baseline process identities changed during the batch",
        )
    if require_owned_absent and owned_present:
        _fail(
            "GPU_SMOKE_OWN_GPU_ALLOCATION_REMAINS",
            "guarded service PIDs still hold a GPU allocation",
        )
    return {
        "baseline_process_identities_preserved": True,
        "owned_pids": sorted(owned_pids),
        "owned_gpu_pids_present": owned_present,
        "owned_gpu_allocation_absent": not owned_present,
        "foreign_process_target_count": 0,
    }


def _assert_port_free() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind(("127.0.0.1", 18007))
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PORT_OCCUPIED",
            "loopback port 18007 is already in use; no occupant was signaled",
        ) from exc
    finally:
        probe.close()


def _same_process(actual: ProcessIdentity, expected: ProcessIdentity) -> bool:
    return (
        actual.uid == expected.uid
        and actual.pid == expected.pid
        and actual.ppid == expected.ppid
        and actual.pgid == expected.pgid
        and actual.sid == expected.sid
        and actual.starttime_ticks == expected.starttime_ticks
        and actual.executable_path == expected.executable_path
        and actual.executable_sha256 == expected.executable_sha256
        and actual.argv == expected.argv
    )


def _same_process_except_ppid(actual: ProcessIdentity, expected: ProcessIdentity) -> bool:
    return (
        actual.uid == expected.uid
        and actual.pid == expected.pid
        and actual.pgid == expected.pgid
        and actual.sid == expected.sid
        and actual.starttime_ticks == expected.starttime_ticks
        and actual.executable_path == expected.executable_path
        and actual.executable_sha256 == expected.executable_sha256
        and actual.argv == expected.argv
    )


def _read_procdir_bytes(procdir_fd: int, name: str, *, maximum_bytes: int) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=procdir_fd)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or not 0 <= metadata.st_size <= maximum_bytes:
                _fail(
                    "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                    "pinned proc metadata file has an unsafe shape",
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(65_536, maximum_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum_bytes:
                    _fail(
                        "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                        "pinned proc metadata exceeds its read bound",
                    )
            return b"".join(chunks)
        finally:
            os.close(fd)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "pinned proc metadata could not be read through its dirfd",
        ) from exc


def _minimal_identity_from_procdir(
    procdir_fd: int,
    pid: int,
) -> _MinimalDirectChildIdentity:
    try:
        uid = os.fstat(procdir_fd).st_uid
        stat_text = _read_procdir_bytes(procdir_fd, "stat", maximum_bytes=64 * 1024).decode("utf-8")
        close = stat_text.rfind(")")
        fields = stat_text[close + 2 :].split()
        if close < 1 or len(fields) < 20:
            raise ValueError("short proc stat")
        return _MinimalDirectChildIdentity(
            uid=uid,
            pid=pid,
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            sid=int(fields[3]),
            starttime_ticks=int(fields[19]),
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "pinned minimal process identity is malformed",
        ) from exc


def _full_identity_from_procdir(
    procdir_fd: int,
    minimal: _MinimalDirectChildIdentity,
) -> ProcessIdentity:
    argv_bytes = _read_procdir_bytes(procdir_fd, "cmdline", maximum_bytes=4 * 1024 * 1024)
    argv = tuple(
        item.decode("utf-8", errors="surrogateescape") for item in argv_bytes.split(b"\0") if item
    )
    if not argv:
        _fail("GPU_SMOKE_PROCESS_IDENTITY_INVALID", "process argv is empty")
    try:
        executable_path = os.readlink("exe", dir_fd=procdir_fd)
        executable_fd = os.open("exe", os.O_RDONLY | os.O_CLOEXEC, dir_fd=procdir_fd)
        try:
            executable_metadata = os.fstat(executable_fd)
            if not stat.S_ISREG(executable_metadata.st_mode):
                _fail(
                    "GPU_SMOKE_PROCESS_IDENTITY_INVALID",
                    "process executable is not a regular file",
                )
            executable_sha256 = _hash_open_regular_file(executable_fd, executable_metadata)
        finally:
            os.close(executable_fd)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "pinned process executable could not be read through its procdir",
        ) from exc
    return ProcessIdentity(
        minimal.uid,
        minimal.pid,
        minimal.ppid,
        minimal.pgid,
        minimal.sid,
        minimal.starttime_ticks,
        executable_path,
        executable_sha256,
        argv,
    )


def _open_owned_process_handles(pid: int, owner_uid: int) -> tuple[int, int] | None:
    """Pin pidfd and procdir; reject foreign UID before any proc member read."""

    pidfd = _pidfd_open(pid)
    if pidfd < 0:
        return None
    try:
        procdir_fd = os.open(
            f"/proc/{pid}",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        os.close(pidfd)
        return None
    except OSError as exc:
        os.close(pidfd)
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "pidfd-pinned procdir could not be opened",
        ) from exc
    if os.fstat(procdir_fd).st_uid != owner_uid:
        os.close(procdir_fd)
        os.close(pidfd)
        _fail(
            "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
            "pidfd/procdir-pinned PID has a foreign UID; sensitive proc members were not read",
        )
    if not _pidfd_process_is_live(pidfd):
        os.close(procdir_fd)
        os.close(pidfd)
        return None
    return pidfd, procdir_fd


def _minimal_matches_expected(
    actual: _MinimalDirectChildIdentity,
    expected: ProcessIdentity,
    *,
    allow_ppid_change: bool,
) -> bool:
    return (
        actual.uid == expected.uid
        and actual.pid == expected.pid
        and (allow_ppid_change or actual.ppid == expected.ppid)
        and actual.pgid == expected.pgid
        and actual.sid == expected.sid
        and actual.starttime_ticks == expected.starttime_ticks
    )


def _pin_exact_owned_process(
    expected: ProcessIdentity,
    *,
    allow_ppid_change: bool = False,
) -> tuple[int, int, ProcessIdentity] | None:
    handles = _open_owned_process_handles(expected.pid, expected.uid)
    if handles is None:
        return None
    pidfd, procdir_fd = handles
    try:
        minimal_before = _minimal_identity_from_procdir(procdir_fd, expected.pid)
        if not _minimal_matches_expected(
            minimal_before,
            expected,
            allow_ppid_change=allow_ppid_change,
        ):
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "pinned PID minimal identity differs before sensitive proc reads",
            )
        minimal_confirmed = _minimal_identity_from_procdir(procdir_fd, expected.pid)
        if minimal_confirmed != minimal_before:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "pinned PID minimal identity changed before sensitive proc reads",
            )
        identity = _full_identity_from_procdir(procdir_fd, minimal_before)
        minimal_after = _minimal_identity_from_procdir(procdir_fd, expected.pid)
        if minimal_after != minimal_before or not (
            _same_process(identity, expected)
            or (allow_ppid_change and _same_process_except_ppid(identity, expected))
        ):
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "pinned PID changed during sensitive proc reads",
            )
        return pidfd, procdir_fd, identity
    except BaseException:
        os.close(procdir_fd)
        os.close(pidfd)
        raise


def _read_exact_owned_process_identity(
    expected: ProcessIdentity,
    *,
    allow_ppid_change: bool = False,
) -> ProcessIdentity | None:
    pinned = _pin_exact_owned_process(expected, allow_ppid_change=allow_ppid_change)
    if pinned is None:
        return None
    pidfd, procdir_fd, identity = pinned
    os.close(procdir_fd)
    os.close(pidfd)
    return identity


def _read_new_owned_child_identity(
    child_pid: int,
    parent: ProcessIdentity,
    launch_root: ProcessIdentity,
) -> ProcessIdentity | None:
    handles = _open_owned_process_handles(child_pid, launch_root.uid)
    if handles is None:
        return None
    pidfd, procdir_fd = handles
    try:
        minimal = _minimal_identity_from_procdir(procdir_fd, child_pid)
        if not (
            minimal.uid == launch_root.uid
            and minimal.ppid == parent.pid
            and minimal.sid == launch_root.sid
            and minimal.starttime_ticks >= launch_root.starttime_ticks
        ):
            _fail(
                "GPU_SMOKE_PROCESS_TREE_INVALID",
                "task-child PID failed minimal ancestry proof before sensitive proc reads",
            )
        if _minimal_identity_from_procdir(procdir_fd, child_pid) != minimal:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "task-child minimal identity changed before sensitive proc reads",
            )
        identity = _full_identity_from_procdir(procdir_fd, minimal)
        if _minimal_identity_from_procdir(procdir_fd, child_pid) != minimal:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "task-child changed during sensitive proc reads",
            )
        return identity
    finally:
        os.close(procdir_fd)
        os.close(pidfd)


def _read_owned_identity_matching_minimal(
    expected: _MinimalDirectChildIdentity,
) -> ProcessIdentity | None:
    """Promote one already pinned direct-child identity without a PID-path race."""

    handles = _open_owned_process_handles(expected.pid, expected.uid)
    if handles is None:
        return None
    pidfd, procdir_fd = handles
    try:
        minimal_before = _minimal_identity_from_procdir(procdir_fd, expected.pid)
        if minimal_before != expected:
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "direct-child minimal identity changed before sensitive proc reads",
            )
        minimal_confirmed = _minimal_identity_from_procdir(procdir_fd, expected.pid)
        if minimal_confirmed != minimal_before:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "direct-child minimal identity changed before promotion",
            )
        identity = _full_identity_from_procdir(procdir_fd, minimal_before)
        minimal_after = _minimal_identity_from_procdir(procdir_fd, expected.pid)
        if minimal_after != minimal_before:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "direct-child identity changed during sensitive proc reads",
            )
        return identity
    finally:
        os.close(procdir_fd)
        os.close(pidfd)


def _owned_task_children(expected: ProcessIdentity) -> tuple[int, ...]:
    """Read only Linux child links rooted at an already proven owned PID."""

    pinned = _pin_exact_owned_process(expected)
    if pinned is None:
        return ()
    pidfd, procdir_fd, _identity = pinned
    try:
        try:
            task_fd = os.open(
                "task",
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=procdir_fd,
            )
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                "owned process task directory cannot be read",
            ) from exc
        try:
            children: set[int] = set()
            for task_name in sorted(os.listdir(task_fd)):
                if not task_name.isdecimal():
                    continue
                try:
                    thread_fd = os.open(
                        task_name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=task_fd,
                    )
                except FileNotFoundError:
                    continue
                try:
                    raw = _read_procdir_bytes(
                        thread_fd,
                        "children",
                        maximum_bytes=1024 * 1024,
                    ).decode("ascii")
                except GpuLiveSmokeError as exc:
                    if exc.__cause__ is not None and isinstance(exc.__cause__, FileNotFoundError):
                        continue
                    raise
                finally:
                    os.close(thread_fd)
                try:
                    candidates = [int(item) for item in raw.split()]
                except ValueError as exc:
                    raise GpuLiveSmokeError(
                        "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                        "owned task children list is malformed",
                    ) from exc
                if any(candidate <= 1 for candidate in candidates):
                    _fail("GPU_SMOKE_PROCESS_IDENTITY_INVALID", "unsafe child PID")
                children.update(candidates)
            minimal_after = _minimal_identity_from_procdir(procdir_fd, expected.pid)
            if not _minimal_matches_expected(
                minimal_after,
                expected,
                allow_ppid_change=False,
            ):
                _fail(
                    "GPU_SMOKE_PROCESS_TREE_DRIFT",
                    "owned parent changed during child traversal",
                )
            return tuple(sorted(children))
        finally:
            os.close(task_fd)
    finally:
        os.close(procdir_fd)
        os.close(pidfd)


def _discover_owned_process_tree(root: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
    """Discover descendants through owned task child links, never global /proc."""

    actual_root = _read_exact_owned_process_identity(root)
    if actual_root is None:
        _fail(
            "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
            "service root exited while discovering descendants",
        )
    if not _same_process(actual_root, root):
        _fail(
            "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
            "service root identity changed while discovering descendants",
        )
    by_pid: dict[int, ProcessIdentity] = {root.pid: actual_root}
    queue = [actual_root]
    while queue:
        parent = queue.pop(0)
        for child_pid in _owned_task_children(parent):
            if child_pid in by_pid:
                continue
            child = _read_new_owned_child_identity(child_pid, parent, root)
            if child is None:
                continue
            if not (
                child.uid == root.uid
                and child.ppid == parent.pid
                and child.sid == root.sid
                and child.starttime_ticks >= root.starttime_ticks
            ):
                _fail(
                    "GPU_SMOKE_PROCESS_TREE_INVALID",
                    "owned task child is not an exact launch-root descendant",
                )
            by_pid[child.pid] = child
            queue.append(child)
    return tuple(sorted(by_pid.values(), key=lambda item: item.pid))


def _current_recorded_process_tree(
    recorded_tree: tuple[ProcessIdentity, ...],
    *,
    allow_reparented_descendants: bool = False,
) -> tuple[ProcessIdentity, ...]:
    """Revalidate a frozen owned tree and reject every unrecorded child."""

    recorded = {item.pid: item for item in recorded_tree}
    roots = [item for item in recorded_tree if item.ppid not in recorded]
    if len(roots) != 1:
        _fail("GPU_SMOKE_PROCESS_TREE_INVALID", "recorded tree lacks one exact launch root")
    launch_root = roots[0]
    current: dict[int, ProcessIdentity] = {}
    observed: dict[int, ProcessIdentity] = {}
    for expected in recorded_tree:
        actual = _read_exact_owned_process_identity(
            expected,
            allow_ppid_change=allow_reparented_descendants,
        )
        if actual is None:
            continue
        observed[actual.pid] = actual
    for expected in recorded_tree:
        actual = observed.get(expected.pid)
        if actual is None:
            continue
        recorded_parent_exited = expected.ppid in recorded and expected.ppid not in observed
        reparented_exact_descendant = (
            allow_reparented_descendants
            and recorded_parent_exited
            and actual.ppid != expected.ppid
            and _same_process_except_ppid(actual, expected)
        )
        if not (_same_process(actual, expected) or reparented_exact_descendant):
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "recorded service-tree PID changed identity",
            )
        current[actual.pid] = actual
    for member in tuple(current.values()):
        for child_pid in _owned_task_children(member):
            expected_child = recorded.get(child_pid)
            if expected_child is None:
                # Establish that the newly observed child is same-UID before
                # classifying drift; a foreign child is never inspected.
                child = _read_new_owned_child_identity(child_pid, member, launch_root)
                if child is None:
                    continue
                _fail(
                    "GPU_SMOKE_PROCESS_TREE_DRIFT",
                    "unrecorded owned child exists; no process was signaled",
                )
            actual_child = _read_exact_owned_process_identity(expected_child)
            if actual_child is None:
                continue
            if not _same_process(actual_child, expected_child):
                _fail(
                    "GPU_SMOKE_PROCESS_TREE_DRIFT",
                    "recorded child changed identity",
                )
    return tuple(sorted(current.values(), key=lambda item: item.pid))


def _validate_guard_binding(guard: ProcessOwnershipGuard) -> None:
    root = guard.root
    if not (
        root.uid == os.getuid()
        and guard.model_id in MODEL_ORDER
        and guard.gpu_uuid == AUTHORIZED_GPU_UUID
        and guard.host == "127.0.0.1"
        and guard.port == 18007
        and guard.expected_argv == root.argv
        and _is_sha256(guard.environment_sha256)
        and guard.snapshot_path in root.argv
        and guard.served_name in root.argv
        and "--host" in root.argv
        and "127.0.0.1" in root.argv
        and "--port" in root.argv
        and "18007" in root.argv
    ):
        _fail(
            "GPU_SMOKE_PROCESS_GUARD_INVALID",
            "service guard lost its exact UID/model/GPU/port/argv binding",
        )


def _validate_owned_members(
    guard: ProcessOwnershipGuard,
    *,
    require_root: bool,
    allow_reparented_descendants: bool = False,
) -> tuple[ProcessIdentity, ...]:
    _validate_guard_binding(guard)
    if not guard.service_tree:
        _fail(
            "GPU_SMOKE_PROCESS_TREE_UNBOUND",
            "service tree is not frozen; no process may be signaled",
        )
    members = _current_recorded_process_tree(
        guard.service_tree,
        allow_reparented_descendants=allow_reparented_descendants,
    )
    by_pid = {member.pid: member for member in members}
    root = by_pid.get(guard.root.pid)
    if require_root and (root is None or not _same_process(root, guard.root)):
        _fail(
            "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
            "service root identity changed; no process was signaled",
        )
    for member in members:
        if (
            member.uid != guard.root.uid
            or member.sid != guard.root.sid
            or member.starttime_ticks < guard.root.starttime_ticks
        ):
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "process group contains a non-owned identity; no process was signaled",
            )
    return members


def _bind_service_tree(guard: ProcessOwnershipGuard) -> ProcessOwnershipGuard:
    _validate_guard_binding(guard)
    members = _discover_owned_process_tree(guard.root)
    recorded = {item.pid: item for item in guard.service_tree}
    for member in members:
        previous = recorded.get(member.pid)
        if previous is not None and not _same_process(member, previous):
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "recorded service-tree PID changed identity",
            )
    return replace(guard, service_tree=members)


def _listening_socket_inodes() -> set[str]:
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            local = fields[1]
            try:
                address_hex, port_hex = local.rsplit(":", 1)
                port = int(port_hex, 16)
            except ValueError:
                continue
            loopback = address_hex in {"0100007F", "00000000000000000000000001000000"}
            if loopback and port == 18007:
                inodes.add(fields[9])
    return inodes


def _socket_inodes_for_members(
    members: tuple[ProcessIdentity, ...],
) -> set[str]:
    """Read only pidfd/procdir-pinned, fully revalidated owned FD trees."""

    inodes: set[str] = set()
    for expected in members:
        pinned = _pin_exact_owned_process(expected)
        if pinned is None:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "owned member exited before its socket census could be pinned",
            )
        pidfd, procdir_fd, _identity = pinned
        try:
            try:
                fd_root_fd = os.open(
                    "fd",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=procdir_fd,
                )
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_PROCESS_TREE_DRIFT",
                    "owned member FD directory disappeared during socket census",
                ) from exc
            try:
                entries = tuple(sorted(os.listdir(fd_root_fd)))
                for entry in entries:
                    if not entry.isdecimal():
                        _fail(
                            "GPU_SMOKE_PROCESS_TREE_DRIFT",
                            "owned member FD directory contains a malformed entry",
                        )
                    try:
                        target = os.readlink(entry, dir_fd=fd_root_fd)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise GpuLiveSmokeError(
                            "GPU_SMOKE_PROCESS_TREE_DRIFT",
                            "owned member FD link could not be censused",
                        ) from exc
                    match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
                    if match:
                        inodes.add(match.group(1))
            finally:
                os.close(fd_root_fd)
            minimal_after = _minimal_identity_from_procdir(procdir_fd, expected.pid)
            if not _minimal_matches_expected(
                minimal_after,
                expected,
                allow_ppid_change=False,
            ):
                _fail(
                    "GPU_SMOKE_PROCESS_TREE_DRIFT",
                    "owned member minimal identity changed during socket census",
                )
            full_after = _full_identity_from_procdir(procdir_fd, minimal_after)
            final_minimal = _minimal_identity_from_procdir(procdir_fd, expected.pid)
            if final_minimal != minimal_after or not _same_process(full_after, expected):
                _fail(
                    "GPU_SMOKE_PROCESS_TREE_DRIFT",
                    "owned member identity changed during socket census",
                )
        finally:
            os.close(procdir_fd)
            os.close(pidfd)
    return inodes


def _inspect_owned_inet_sockets(guard: ProcessOwnershipGuard) -> dict[str, JsonValue]:
    """Census INET sockets reachable from only the frozen owned service FDs."""

    members = _validate_owned_members(guard, require_root=True)
    owned_pids = {member.pid for member in members}
    owned_inodes = _socket_inodes_for_members(members)
    rows: list[dict[str, JsonValue]] = []
    tables = (
        ("tcp4", "/proc/net/tcp"),
        ("tcp6", "/proc/net/tcp6"),
        ("udp4", "/proc/net/udp"),
        ("udp6", "/proc/net/udp6"),
    )
    loopback_addresses = {"0100007F", "00000000000000000000000001000000"}
    unspecified_addresses = {"00000000", "00000000000000000000000000000000"}
    for protocol, table in tables:
        try:
            lines = Path(table).read_text(encoding="ascii").splitlines()[1:]
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                "owned INET socket table cannot be read",
            ) from exc
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[9] not in owned_inodes:
                continue
            try:
                local_address, local_port_hex = fields[1].rsplit(":", 1)
                remote_address, remote_port_hex = fields[2].rsplit(":", 1)
                local_port = int(local_port_hex, 16)
                remote_port = int(remote_port_hex, 16)
            except ValueError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                    "owned INET socket row is malformed",
                ) from exc
            local_scope = (
                "LOOPBACK"
                if local_address in loopback_addresses
                else "UNSPECIFIED"
                if local_address in unspecified_addresses
                else "NON_LOOPBACK"
            )
            remote_scope = (
                "LOOPBACK"
                if remote_address in loopback_addresses
                else "UNSPECIFIED"
                if remote_address in unspecified_addresses
                else "NON_LOOPBACK"
            )
            loopback = local_scope == "LOOPBACK" and remote_scope in {"LOOPBACK", "UNSPECIFIED"}
            rows.append(
                {
                    "protocol": protocol,
                    "state_hex": fields[3],
                    "local_port": local_port,
                    "remote_port": remote_port,
                    "socket_inode": fields[9],
                    "local_scope": local_scope,
                    "remote_scope": remote_scope,
                    "loopback_only": loopback,
                }
            )
    rows.sort(
        key=lambda item: (
            cast(str, item["protocol"]),
            cast(int, item["local_port"]),
            cast(int, item["remote_port"]),
            cast(str, item["socket_inode"]),
        )
    )
    non_loopback = sum(item["loopback_only"] is False for item in rows)
    return {
        "owned_pids": sorted(owned_pids),
        "inet_sockets": cast(JsonValue, rows),
        "inet_socket_count": len(rows),
        "non_loopback_inet_socket_count": non_loopback,
        "foreign_process_fds_read": 0,
    }


def _assert_listener_owned(
    guard: ProcessOwnershipGuard, *, allow_tree_extension: bool = False
) -> tuple[ProcessIdentity, ...]:
    members = (
        _bind_service_tree(guard).service_tree
        if allow_tree_extension or not guard.service_tree
        else _validate_owned_members(guard, require_root=True)
    )
    _assert_listener_owned_by_members(members)
    return members


def _assert_listener_owned_by_members(
    members: tuple[ProcessIdentity, ...],
) -> None:
    listeners = _listening_socket_inodes()
    owned = _socket_inodes_for_members(members)
    if not listeners or not listeners.issubset(owned):
        _fail(
            "GPU_SMOKE_PORT_OWNERSHIP_MISMATCH",
            "port 18007 listener is not wholly owned by the guarded service group",
        )


def _capture_guard(
    process: subprocess.Popen[bytes],
    *,
    expected_argv: tuple[str, ...],
    model_id: str,
    snapshot_path: str,
    served_name: str,
    owner_uid: int,
    expected_executable_path: str,
    expected_executable_sha256: str,
    environment_sha256: str,
    expected_root: ProcessIdentity | None = None,
) -> ProcessOwnershipGuard:
    if expected_root is None:
        minimal = _capture_minimal_direct_child(process, owner_uid)
        identity = _read_owned_identity_matching_minimal(minimal)
    else:
        identity = _read_exact_owned_process_identity(expected_root)
    if identity is None:
        _fail(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "server root exited before its full guard could be captured",
        )
    resolved_executable = str(Path(expected_executable_path).resolve(strict=True))
    if not (
        identity.uid == owner_uid == os.getuid()
        and identity.ppid == os.getpid()
        and identity.pid == identity.pgid
        and identity.pid == identity.sid
        and identity.executable_path == resolved_executable
        and identity.executable_sha256 == expected_executable_sha256
        and identity.argv == expected_argv
        and snapshot_path in identity.argv
        and served_name in identity.argv
        and "--host" in identity.argv
        and "127.0.0.1" in identity.argv
        and "--port" in identity.argv
        and "18007" in identity.argv
        and _is_sha256(environment_sha256)
    ):
        _fail(
            "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
            "launched service identity does not match exact UID/PID/PGID/argv/model/port",
        )
    return ProcessOwnershipGuard(
        root=identity,
        model_id=model_id,
        snapshot_path=snapshot_path,
        served_name=served_name,
        host="127.0.0.1",
        port=18007,
        expected_argv=expected_argv,
        environment_sha256=environment_sha256,
        gpu_uuid=AUTHORIZED_GPU_UUID,
    )


def _pidfd_syscall(number: int, *arguments: object) -> int:
    uname = os.uname()
    if uname.sysname != "Linux" or uname.machine != "x86_64":
        _fail(
            "GPU_SMOKE_RUNTIME_INVALID",
            "pidfd signaling is frozen to Linux x86_64",
        )
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    result = cast(int, syscall(ctypes.c_long(number), *arguments))
    if result < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return result


def _pidfd_open(pid: int) -> int:
    try:
        return _pidfd_syscall(
            _PIDFD_OPEN_SYSCALL_X86_64,
            ctypes.c_int(pid),
            ctypes.c_uint(0),
        )
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return -1
        raise GpuLiveSmokeError(
            "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED",
            "pidfd_open failed; no PID signal was attempted",
        ) from exc


def _pidfd_send_signal(pidfd: int, sig: signal.Signals) -> bool:
    try:
        _pidfd_syscall(
            _PIDFD_SEND_SIGNAL_SYSCALL_X86_64,
            ctypes.c_int(pidfd),
            ctypes.c_int(int(sig)),
            ctypes.c_void_p(),
            ctypes.c_uint(0),
        )
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise GpuLiveSmokeError(
            "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED",
            "pidfd_send_signal failed",
        ) from exc
    return True


def _pidfd_process_is_live(pidfd: int) -> bool:
    """Observe pidfd exit readiness without sending even signal zero."""

    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        return not poller.poll(0)
    except (OSError, ValueError) as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "acquisition pidfd liveness could not be observed without signaling",
        ) from exc


def _owned_command_trace_event(
    trace: list[dict[str, JsonValue]],
    member: _PinnedOwnedCommandMember | None,
    *,
    pid: int,
    state: str,
    reason: str,
) -> None:
    trace.append(
        {
            "sequence": len(trace) + 1,
            "pid": pid,
            "starttime_ticks": (member.identity.starttime_ticks if member is not None else None),
            "signal": "SIGKILL",
            "state": state,
            "signal_api": "PIDFD",
            "ownership": "RECORDED_OWN_ACQUISITION",
            "reason": reason,
            "scope": "OWNED_AUXILIARY_COMMAND",
        }
    )


def _pinned_command_minimal(
    member: _PinnedOwnedCommandMember,
    *,
    allow_ppid_change: bool,
) -> _MinimalDirectChildIdentity | None:
    if not _pidfd_process_is_live(member.pidfd):
        return None
    if os.fstat(member.procdir_fd).st_uid != member.identity.uid:
        _fail(
            "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
            "pinned auxiliary-command procdir changed owner",
        )
    before = _minimal_identity_from_procdir(member.procdir_fd, member.identity.pid)
    if not _minimal_matches_expected(
        before,
        member.identity,
        allow_ppid_change=allow_ppid_change,
    ):
        _fail(
            "GPU_SMOKE_PROCESS_TREE_DRIFT",
            "pinned auxiliary-command identity changed",
        )
    after = _minimal_identity_from_procdir(member.procdir_fd, member.identity.pid)
    if after != before:
        _fail(
            "GPU_SMOKE_PROCESS_TREE_DRIFT",
            "pinned auxiliary-command identity changed during observation",
        )
    return before


def _pinned_command_full_identity(
    member: _PinnedOwnedCommandMember,
    *,
    allow_ppid_change: bool,
) -> ProcessIdentity | None:
    minimal = _pinned_command_minimal(
        member,
        allow_ppid_change=allow_ppid_change,
    )
    if minimal is None:
        return None
    identity = _full_identity_from_procdir(member.procdir_fd, minimal)
    after = _minimal_identity_from_procdir(member.procdir_fd, member.identity.pid)
    original_matches = _same_process(identity, member.identity) or (
        allow_ppid_change and _same_process_except_ppid(identity, member.identity)
    )
    allowed_exec_matches = (
        member.allowed_exec_argv is not None
        and member.allowed_exec_path is not None
        and member.allowed_exec_sha256 is not None
        and identity.argv == member.allowed_exec_argv
        and identity.executable_path == member.allowed_exec_path
        and identity.executable_sha256 == member.allowed_exec_sha256
        and identity.uid == member.identity.uid
        and identity.pid == member.identity.pid
        and (allow_ppid_change or identity.ppid == member.identity.ppid)
        and identity.pgid == member.identity.pgid
        and identity.sid == member.identity.sid
        and identity.starttime_ticks == member.identity.starttime_ticks
    )
    if after != minimal or not (original_matches or allowed_exec_matches):
        _fail(
            "GPU_SMOKE_PROCESS_TREE_DRIFT",
            "pinned auxiliary-command full identity changed",
        )
    return identity


def _capture_owned_command_root(
    process: subprocess.Popen[bytes],
    acquisition_pidfd: int,
    command: tuple[str, ...],
) -> _PinnedOwnedCommandMember | None:
    """Promote the Popen direct child while its first pidfd remains held."""

    try:
        procdir_fd = os.open(
            f"/proc/{process.pid}",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        if not _pidfd_process_is_live(acquisition_pidfd):
            return None
        _fail(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "live auxiliary-command child lacks a pinned procdir",
        )
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "auxiliary-command procdir could not be pinned",
        ) from exc
    try:
        if os.fstat(procdir_fd).st_uid != os.getuid():
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "auxiliary-command direct child has a foreign UID",
            )
        if not _pidfd_process_is_live(acquisition_pidfd):
            os.close(procdir_fd)
            return None
        minimal = _minimal_identity_from_procdir(procdir_fd, process.pid)
        if _minimal_identity_from_procdir(procdir_fd, process.pid) != minimal:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "auxiliary-command direct child changed before promotion",
            )
        if not (
            minimal.uid == os.getuid()
            and minimal.pid == process.pid
            and minimal.ppid == os.getpid()
            and minimal.pgid == minimal.pid
            and minimal.sid == minimal.pid
        ):
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "auxiliary command is not the exact Popen direct-child/session root",
            )
        identity = _full_identity_from_procdir(procdir_fd, minimal)
        if _minimal_identity_from_procdir(procdir_fd, process.pid) != minimal:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "auxiliary-command direct child changed during promotion",
            )
        expected_executable = os.path.realpath(command[0])
        if not os.path.isabs(expected_executable):
            _fail(
                "GPU_SMOKE_RUNTIME_INVALID",
                "auxiliary-command executable cannot be resolved",
            )
        expected_sha256, _expected_bytes = _hash_regular_file(expected_executable)
        if not (
            identity.argv == command
            and identity.executable_path == expected_executable
            and identity.executable_sha256 == expected_sha256
        ):
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "auxiliary-command argv/executable differs from the Popen intent",
            )
        return _PinnedOwnedCommandMember(
            identity=identity,
            pidfd=acquisition_pidfd,
            procdir_fd=procdir_fd,
            depth=0,
        )
    except BaseException:
        if not _pidfd_process_is_live(acquisition_pidfd):
            os.close(procdir_fd)
            return None
        os.close(procdir_fd)
        raise


def _pinned_command_child_pids(member: _PinnedOwnedCommandMember) -> tuple[int, ...]:
    if _pinned_command_minimal(member, allow_ppid_change=False) is None:
        return ()
    try:
        task_fd = os.open(
            "task",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=member.procdir_fd,
        )
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "auxiliary-command task directory cannot be pinned",
        ) from exc
    try:
        children: set[int] = set()
        for task_name in sorted(os.listdir(task_fd)):
            if not task_name.isdecimal():
                continue
            try:
                thread_fd = os.open(
                    task_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=task_fd,
                )
            except FileNotFoundError:
                continue
            try:
                raw = _read_procdir_bytes(
                    thread_fd,
                    "children",
                    maximum_bytes=1024 * 1024,
                ).decode("ascii")
            finally:
                os.close(thread_fd)
            try:
                candidates = [int(item) for item in raw.split()]
            except ValueError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                    "auxiliary-command children list is malformed",
                ) from exc
            if any(candidate <= 1 for candidate in candidates):
                _fail("GPU_SMOKE_PROCESS_IDENTITY_INVALID", "unsafe auxiliary child PID")
            children.update(candidates)
        if _pinned_command_minimal(member, allow_ppid_change=False) is None:
            return ()
        return tuple(sorted(children))
    finally:
        os.close(task_fd)


def _capture_owned_command_child(
    child_pid: int,
    parent: _PinnedOwnedCommandMember,
    root: _PinnedOwnedCommandMember,
) -> _PinnedOwnedCommandMember | None:
    handles = _open_owned_process_handles(child_pid, root.identity.uid)
    if handles is None:
        return None
    pidfd, procdir_fd = handles
    try:
        minimal = _minimal_identity_from_procdir(procdir_fd, child_pid)
        if not (
            minimal.uid == root.identity.uid
            and minimal.ppid == parent.identity.pid
            and minimal.sid == root.identity.sid
            and minimal.starttime_ticks >= root.identity.starttime_ticks
        ):
            _fail(
                "GPU_SMOKE_PROCESS_TREE_INVALID",
                "auxiliary-command child failed pinned ancestry proof",
            )
        if _minimal_identity_from_procdir(procdir_fd, child_pid) != minimal:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "auxiliary-command child changed before promotion",
            )
        identity = _full_identity_from_procdir(procdir_fd, minimal)
        if _minimal_identity_from_procdir(procdir_fd, child_pid) != minimal:
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "auxiliary-command child changed during promotion",
            )
        return _PinnedOwnedCommandMember(
            identity=identity,
            pidfd=pidfd,
            procdir_fd=procdir_fd,
            depth=parent.depth + 1,
        )
    except BaseException:
        os.close(procdir_fd)
        os.close(pidfd)
        raise


def _observe_owned_command_descendants(
    members: dict[int, _PinnedOwnedCommandMember],
) -> tuple[_PinnedOwnedCommandMember, ...]:
    root = min(members.values(), key=lambda item: item.depth)
    discovered: list[_PinnedOwnedCommandMember] = []
    queue = sorted(members.values(), key=lambda item: (item.depth, item.identity.pid))
    while queue:
        parent = queue.pop(0)
        for child_pid in _pinned_command_child_pids(parent):
            if child_pid in members:
                continue
            child = _capture_owned_command_child(child_pid, parent, root)
            if child is None:
                continue
            members[child.identity.pid] = child
            discovered.append(child)
            queue.append(child)
    return tuple(discovered)


def _terminate_pinned_owned_command(
    members: dict[int, _PinnedOwnedCommandMember],
    trace: list[dict[str, JsonValue]],
    *,
    reason: str,
) -> bool:
    """SIGKILL only exact still-live pinned members, descendants first."""

    for member in sorted(
        members.values(),
        key=lambda item: (item.depth, item.identity.pid),
        reverse=True,
    ):
        _owned_command_trace_event(
            trace,
            member,
            pid=member.identity.pid,
            state="INTENDED",
            reason=reason,
        )
        # The pidfd and procdir are retained from the proven auxiliary-command
        # acquisition.  A subsequently observed fork child may legitimately
        # exec before cleanup, so argv/executable are not stable cleanup
        # identity for this narrowly scoped helper tree.  Revalidate the
        # stable minimal tuple only; the held pidfd remains the signal target.
        actual = _pinned_command_minimal(member, allow_ppid_change=True)
        if actual is None:
            _owned_command_trace_event(
                trace,
                member,
                pid=member.identity.pid,
                state="EXITED_BEFORE_SIGNAL",
                reason=reason,
            )
            continue
        _owned_command_trace_event(
            trace,
            member,
            pid=member.identity.pid,
            state="IDENTITY_REVALIDATED",
            reason=reason,
        )
        if _pidfd_send_signal(member.pidfd, signal.SIGKILL):
            _owned_command_trace_event(
                trace,
                member,
                pid=member.identity.pid,
                state="SENT",
                reason=reason,
            )
        else:
            _owned_command_trace_event(
                trace,
                member,
                pid=member.identity.pid,
                state="EXITED_BEFORE_SIGNAL",
                reason=reason,
            )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if all(not _pidfd_process_is_live(item.pidfd) for item in members.values()):
            return True
        time.sleep(0.005)
    return all(not _pidfd_process_is_live(item.pidfd) for item in members.values())


def _owned_command_receipt(
    *,
    command: tuple[str, ...],
    timeout_seconds: float,
    stdout_byte_cap: int,
    stderr_byte_cap: int,
    stdout: bytes,
    stderr: bytes,
    root: _PinnedOwnedCommandMember | None,
    members: dict[int, _PinnedOwnedCommandMember],
    initial_pidfd_acquired: bool,
    launch_gate_released: bool,
    completion_reason: str,
    returncode: int | None,
    signal_trace: list[dict[str, JsonValue]],
    release_proven: bool,
) -> dict[str, JsonValue]:
    return {
        "schema_version": _OWNED_COMMAND_RECEIPT_SCHEMA_VERSION,
        "command": list(command),
        "command_sha256": _sha256(canonical_json_bytes(list(command))),
        "executable_path": command[0],
        "timeout_milliseconds": int(timeout_seconds * 1000),
        "stdout_byte_cap": stdout_byte_cap,
        "stderr_byte_cap": stderr_byte_cap,
        "stdout_byte_count": len(stdout),
        "stderr_byte_count": len(stderr),
        "stdout_sha256": _sha256(stdout),
        "stderr_sha256": _sha256(stderr),
        "initial_pidfd_acquired": initial_pidfd_acquired,
        "launch_gate_identity": root.identity.to_dict() if root is not None else None,
        "launch_gate_code_sha256": _sha256(_OWNED_COMMAND_GATE_CODE.encode("utf-8")),
        "launch_gate_released_after_identity_proof": launch_gate_released,
        "target_executable_path": (
            root.allowed_exec_path if root is not None else os.path.realpath(command[0])
        ),
        "target_executable_sha256": (root.allowed_exec_sha256 if root is not None else None),
        "descendant_policy": "FORBIDDEN",
        "observed_descendant_count": max(0, len(members) - (1 if root is not None else 0)),
        "observed_descendant_identities": [
            item.identity.to_dict()
            for item in sorted(
                members.values(),
                key=lambda member: (member.depth, member.identity.pid),
            )
            if item.depth > 0
        ],
        "descendant_census_max_interval_milliseconds": 10,
        "completion_reason": completion_reason,
        "returncode": returncode,
        "signal_trace": cast(JsonValue, signal_trace),
        "release_proven": release_proven,
        "numeric_pid_signal_count": 0,
        "popen_kill_count": 0,
        "popen_send_signal_count": 0,
        "communicate_timeout_count": 0,
    }


def _run_owned_command(
    command: tuple[str, ...] | list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
    error_code: str,
    stdout_byte_cap: int = 4 * 1024 * 1024,
    stderr_byte_cap: int = 4 * 1024 * 1024,
) -> _OwnedCommandResult:
    """Run one bounded helper without any numeric-PID timeout signal path."""

    frozen_command = tuple(command)
    if not (
        frozen_command
        and all(type(item) is str and item for item in frozen_command)
        and os.path.isabs(frozen_command[0])
        and 0 < timeout_seconds <= 600
        and 0 < stdout_byte_cap <= 64 * 1024 * 1024
        and 0 < stderr_byte_cap <= 64 * 1024 * 1024
    ):
        _fail(error_code, "owned auxiliary-command contract is invalid")
    _scan_for_secrets(cast(JsonValue, list(frozen_command)))
    _scan_for_secrets(cast(JsonValue, env))
    target_environment_bytes = canonical_json_bytes(cast(JsonValue, env))
    target_environment_b64 = base64.urlsafe_b64encode(target_environment_bytes).decode("ascii")
    gate_read_fd, gate_write_fd = os.pipe2(os.O_CLOEXEC)
    gate_python = os.path.abspath(sys.executable)
    gate_command = (
        gate_python,
        *_ISOLATED_PYTHON_FLAGS,
        "-c",
        _OWNED_COMMAND_GATE_CODE,
        str(gate_read_fd),
        target_environment_b64,
        *frozen_command,
    )
    try:
        process = subprocess.Popen(
            list(gate_command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"LC_CTYPE": "C.UTF-8"},
            close_fds=True,
            start_new_session=True,
            shell=False,
            pass_fds=(gate_read_fd,),
        )
    except OSError as exc:
        os.close(gate_read_fd)
        os.close(gate_write_fd)
        receipt = _owned_command_receipt(
            command=frozen_command,
            timeout_seconds=timeout_seconds,
            stdout_byte_cap=stdout_byte_cap,
            stderr_byte_cap=stderr_byte_cap,
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
        error = GpuLiveSmokeError(error_code, "owned auxiliary command could not start")
        error.execution_detail = {"owned_command": receipt}
        raise error from exc
    os.close(gate_read_fd)

    acquisition_pidfd = -1
    root: _PinnedOwnedCommandMember | None = None
    members: dict[int, _PinnedOwnedCommandMember] = {}
    signal_trace: list[dict[str, JsonValue]] = []
    stdout = bytearray()
    stderr = bytearray()
    completion_reason = "INTERNAL_FAILURE"
    release_proven = False
    gate_released = False
    returncode: int | None = None
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[str, BinaryIO]] = {}
    try:
        try:
            acquisition_pidfd = _pidfd_open(process.pid)
        except BaseException as exc:
            receipt = _owned_command_receipt(
                command=frozen_command,
                timeout_seconds=timeout_seconds,
                stdout_byte_cap=stdout_byte_cap,
                stderr_byte_cap=stderr_byte_cap,
                stdout=b"",
                stderr=b"",
                root=None,
                members={},
                initial_pidfd_acquired=False,
                launch_gate_released=False,
                completion_reason="INITIAL_PIDFD_UNAVAILABLE",
                returncode=None,
                signal_trace=[],
                release_proven=False,
            )
            error = GpuLiveSmokeError(
                error_code,
                "initial auxiliary-command pidfd failed; no signal was attempted",
            )
            error.execution_detail = {"owned_command": receipt}
            raise error from exc
        if acquisition_pidfd < 0:
            receipt = _owned_command_receipt(
                command=frozen_command,
                timeout_seconds=timeout_seconds,
                stdout_byte_cap=stdout_byte_cap,
                stderr_byte_cap=stderr_byte_cap,
                stdout=b"",
                stderr=b"",
                root=None,
                members={},
                initial_pidfd_acquired=False,
                launch_gate_released=False,
                completion_reason="EXITED_BEFORE_PIDFD_OPEN",
                returncode=None,
                signal_trace=[],
                release_proven=True,
            )
            error = GpuLiveSmokeError(
                error_code,
                "auxiliary command exited before its initial pidfd was pinned",
            )
            error.execution_detail = {"owned_command": receipt}
            raise error
        root = _capture_owned_command_root(process, acquisition_pidfd, gate_command)
        if root is None:
            completion_reason = "EXITED_BEFORE_IDENTITY_CAPTURE"
            os.close(gate_write_fd)
            gate_write_fd = -1
        else:
            target_executable = os.path.realpath(frozen_command[0])
            target_sha256, _target_bytes = _hash_regular_file(target_executable)
            root = replace(
                root,
                allowed_exec_argv=frozen_command,
                allowed_exec_path=target_executable,
                allowed_exec_sha256=target_sha256,
            )
            members[root.identity.pid] = root
            if os.write(gate_write_fd, b"G") != 1:
                _fail(error_code, "auxiliary-command launch gate could not be released")
            gate_released = True
            os.close(gate_write_fd)
            gate_write_fd = -1

        assert process.stdout is not None
        assert process.stderr is not None
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
            streams[stream.fileno()] = (name, stream)

        deadline = time.monotonic() + timeout_seconds
        failure_reason: str | None = None
        root_live = root is not None and _pidfd_process_is_live(root.pidfd)
        while streams or root_live:
            iteration_started = time.monotonic()
            if root is not None and root_live:
                newly_discovered = _observe_owned_command_descendants(members)
                if newly_discovered:
                    failure_reason = "UNEXPECTED_DESCENDANT"
                    break
            remaining = deadline - iteration_started
            if remaining <= 0:
                failure_reason = "TIMEOUT"
                break
            events = selector.select(min(0.005, remaining))
            for key, _mask in events:
                fd = key.fileobj.fileno()
                name, stream = streams[fd]
                destination = stdout if name == "stdout" else stderr
                cap = stdout_byte_cap if name == "stdout" else stderr_byte_cap
                try:
                    chunk = os.read(fd, min(64 * 1024, cap - len(destination) + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    streams.pop(fd)
                    continue
                destination.extend(chunk)
                if len(destination) > cap:
                    del destination[cap:]
                    failure_reason = "STDOUT_LIMIT" if name == "stdout" else "STDERR_LIMIT"
                    break
            if failure_reason is not None:
                break
            root_live = root is not None and _pidfd_process_is_live(root.pidfd)
            if root is None:
                root_live = False
            if not root_live and not streams:
                break

        if failure_reason is not None:
            release_proven = _terminate_pinned_owned_command(
                members,
                signal_trace,
                reason=failure_reason,
            )
            completion_reason = failure_reason
        else:
            release_proven = all(
                not _pidfd_process_is_live(item.pidfd) for item in members.values()
            )
            completion_reason = "EXITED"
        if root is not None and not _pidfd_process_is_live(root.pidfd):
            returncode = process.wait()
        elif (
            root is None
            and acquisition_pidfd >= 0
            and not _pidfd_process_is_live(acquisition_pidfd)
        ):
            returncode = process.wait()

        stdout_bytes = bytes(stdout)
        stderr_bytes = bytes(stderr)
        receipt = _owned_command_receipt(
            command=frozen_command,
            timeout_seconds=timeout_seconds,
            stdout_byte_cap=stdout_byte_cap,
            stderr_byte_cap=stderr_byte_cap,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            root=root,
            members=members,
            initial_pidfd_acquired=True,
            launch_gate_released=gate_released,
            completion_reason=completion_reason,
            returncode=returncode,
            signal_trace=signal_trace,
            release_proven=release_proven,
        )
        if failure_reason is not None or not release_proven or returncode is None:
            error = GpuLiveSmokeError(
                error_code,
                "owned auxiliary command did not complete within its closed boundary",
            )
            error.execution_detail = {"owned_command": receipt}
            raise error
        _scan_bytes_for_credentials(stdout_bytes, artifact_kind="owned_command_stdout")
        _scan_bytes_for_credentials(stderr_bytes, artifact_kind="owned_command_stderr")
        return _OwnedCommandResult(
            returncode=returncode,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            receipt=receipt,
        )
    except GpuLiveSmokeError as exc:
        if exc.execution_detail is None:
            release_proven = (
                release_proven and not _pidfd_process_is_live(acquisition_pidfd)
                if acquisition_pidfd >= 0
                else release_proven
            )
            if acquisition_pidfd >= 0 and _pidfd_process_is_live(acquisition_pidfd):
                if root is not None:
                    release_proven = _terminate_pinned_owned_command(
                        members,
                        signal_trace,
                        reason="INTERNAL_FAILURE",
                    )
            receipt = _owned_command_receipt(
                command=frozen_command,
                timeout_seconds=timeout_seconds,
                stdout_byte_cap=stdout_byte_cap,
                stderr_byte_cap=stderr_byte_cap,
                stdout=bytes(stdout),
                stderr=bytes(stderr),
                root=root,
                members=members,
                initial_pidfd_acquired=acquisition_pidfd >= 0,
                launch_gate_released=gate_released,
                completion_reason=completion_reason,
                returncode=returncode,
                signal_trace=signal_trace,
                release_proven=release_proven,
            )
            exc.execution_detail = {"owned_command": receipt}
        raise
    except BaseException as exc:
        release_proven = False
        if acquisition_pidfd >= 0 and _pidfd_process_is_live(acquisition_pidfd):
            if root is not None:
                release_proven = _terminate_pinned_owned_command(
                    members,
                    signal_trace,
                    reason="INTERNAL_FAILURE",
                )
        receipt = _owned_command_receipt(
            command=frozen_command,
            timeout_seconds=timeout_seconds,
            stdout_byte_cap=stdout_byte_cap,
            stderr_byte_cap=stderr_byte_cap,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            root=root,
            members=members,
            initial_pidfd_acquired=acquisition_pidfd >= 0,
            launch_gate_released=gate_released,
            completion_reason="INTERNAL_FAILURE",
            returncode=returncode,
            signal_trace=signal_trace,
            release_proven=release_proven,
        )
        error = GpuLiveSmokeError(error_code, "owned auxiliary command failed")
        error.execution_detail = {"owned_command": receipt}
        raise error from exc
    finally:
        if gate_write_fd >= 0:
            try:
                os.close(gate_write_fd)
            except OSError:
                pass
        selector.close()
        for _name, stream in streams.values():
            try:
                stream.close()
            except OSError:
                pass
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if process.stderr is not None and not process.stderr.closed:
            process.stderr.close()
        for member in members.values():
            try:
                os.close(member.procdir_fd)
            except OSError:
                pass
            try:
                os.close(member.pidfd)
            except OSError:
                pass
        if acquisition_pidfd >= 0 and (root is None or acquisition_pidfd != root.pidfd):
            try:
                os.close(acquisition_pidfd)
            except OSError:
                pass


def _verify_pidfd_runtime() -> dict[str, JsonValue]:
    """Smoke the exact harmless pidfd syscall pair before launching a model."""

    pidfd = _pidfd_open(os.getpid())
    if pidfd < 0:
        _fail("GPU_SMOKE_RUNTIME_INVALID", "runner PID disappeared during pidfd probe")
    try:
        if not _pidfd_send_signal(pidfd, cast(signal.Signals, 0)):
            _fail("GPU_SMOKE_RUNTIME_INVALID", "pidfd signal-0 probe lost the runner")
    finally:
        os.close(pidfd)
    return {
        "platform": "linux-x86_64",
        "pidfd_open_syscall": _PIDFD_OPEN_SYSCALL_X86_64,
        "pidfd_send_signal_syscall": _PIDFD_SEND_SIGNAL_SYSCALL_X86_64,
        "signal_zero_probe_passed": True,
        "os_kill_fallback_allowed": False,
    }


def _signal_trace_event(
    trace: list[dict[str, JsonValue]],
    expected: ProcessIdentity,
    sig: signal.Signals,
    state: str,
    *,
    ownership: str | None = None,
) -> None:
    trace.append(
        {
            "sequence": len(trace) + 1,
            "pid": expected.pid,
            "starttime_ticks": expected.starttime_ticks,
            "signal": sig.name,
            "state": state,
            "signal_api": "PIDFD",
            "ownership": (
                ownership
                if ownership is not None
                else "RECORDED_OWN"
                if expected.uid == os.getuid()
                else "REJECTED_FOREIGN"
            ),
        }
    )


def _raise_with_signal_trace(
    caught: BaseException,
    trace: list[dict[str, JsonValue]],
) -> NoReturn:
    error = (
        caught
        if isinstance(caught, GpuLiveSmokeError)
        else GpuLiveSmokeError(
            "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED",
            f"guarded cleanup failed ({type(caught).__name__})",
        )
    )
    previous = error.execution_detail
    detail: dict[str, JsonValue] = {"signal_trace": cast(JsonValue, trace)}
    if previous is not None:
        detail["prior_detail"] = cast(JsonValue, previous)
    error.execution_detail = detail
    if error is caught:
        raise error
    raise error from caught


def _signal_trace_from_value(value: object) -> list[dict[str, JsonValue]]:
    if type(value) is not dict:
        return []
    mapping = cast(dict[str, object], value)
    raw = mapping.get("signal_trace")
    if type(raw) is list:
        return [cast(dict[str, JsonValue], item) for item in raw if type(item) is dict]
    owned_command = mapping.get("owned_command")
    if type(owned_command) is dict:
        return _signal_trace_from_value(owned_command)
    return _signal_trace_from_value(mapping.get("prior_detail"))


def _owned_command_signal_trace_from_value(value: object) -> list[dict[str, JsonValue]]:
    """Extract only auxiliary-command trace nested in an execution detail."""

    if type(value) is not dict:
        return []
    mapping = cast(dict[str, object], value)
    owned_command = mapping.get("owned_command")
    if type(owned_command) is dict:
        return _signal_trace_from_value(owned_command)
    return _owned_command_signal_trace_from_value(mapping.get("prior_detail"))


def _extend_signal_trace(
    destination: list[dict[str, JsonValue]],
    source: object,
    *,
    cleanup_attempt: str,
) -> None:
    for item in _signal_trace_from_value(source):
        destination.append(
            {
                **item,
                "global_sequence": len(destination) + 1,
                "cleanup_attempt": cleanup_attempt,
            }
        )


def _signal_trace_summary(trace: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    intended = [item for item in trace if item.get("state") == "INTENDED"]
    sent = [item for item in trace if item.get("state") == "SENT"]
    return {
        "signal_intent_count": len(intended),
        "signal_sent_count": len(sent),
        "signal_target_pids": sorted({cast(int, item["pid"]) for item in intended}),
        "signal_sent_pids": sorted({cast(int, item["pid"]) for item in sent}),
        "foreign_process_target_count": len(
            {
                cast(int, item["pid"])
                for item in trace
                if item.get("ownership") == "REJECTED_FOREIGN"
                and item.get("state") in {"INTENDED", "UID_REJECTED"}
            }
        ),
        "broad_process_signal_count": sum(item.get("signal_api") != "PIDFD" for item in intended),
        "pidfd_only": all(item.get("signal_api") == "PIDFD" for item in intended),
    }


def _extend_network_observations(destination: list[dict[str, JsonValue]], value: object) -> None:
    if type(value) is not dict:
        return
    raw = cast(dict[str, object], value).get("request_observations")
    if type(raw) is not list:
        return
    for item in raw:
        if type(item) is dict:
            destination.append(
                {
                    **cast(dict[str, JsonValue], item),
                    "global_sequence": len(destination) + 1,
                }
            )


def _network_observation_summary(
    observations: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    return {
        "request_observation_count": len(observations),
        "exact_loopback_request_count": sum(
            item.get("exact_loopback_allowed") is True for item in observations
        ),
        "non_loopback_connection_count": sum(
            item.get("exact_loopback_allowed") is not True for item in observations
        ),
    }


def _append_socket_observation(
    destination: list[dict[str, JsonValue]],
    census: dict[str, JsonValue],
    *,
    model_id: str,
    phase: str,
    call_id: str | None,
) -> None:
    observation: dict[str, JsonValue] = {
        "ordinal": len(destination) + 1,
        "model_id": model_id,
        "phase": phase,
        "call_id": call_id,
        "census": census,
    }
    destination.append(observation)
    if census.get("non_loopback_inet_socket_count") != 0:
        error = GpuLiveSmokeError(
            "GPU_SMOKE_NON_LOOPBACK_REQUEST_FORBIDDEN",
            "owned service has a non-loopback INET socket",
        )
        error.execution_detail = {"socket_observation": observation}
        raise error


def _socket_observation_summary(
    observations: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    return {
        "socket_observation_count": len(observations),
        "non_loopback_inet_socket_count": sum(
            cast(int, cast(dict[str, JsonValue], item["census"])["non_loopback_inet_socket_count"])
            for item in observations
        ),
    }


def _signal_exact_member(
    expected: ProcessIdentity,
    sig: signal.Signals,
    trace: list[dict[str, JsonValue]] | None = None,
) -> bool:
    signal_trace = [] if trace is None else trace
    _signal_trace_event(signal_trace, expected, sig, "INTENDED")
    if expected.uid != os.getuid():
        _fail(
            "GPU_SMOKE_FOREIGN_PROCESS_SIGNAL_FORBIDDEN",
            "signal target UID is not the runner UID",
        )
    try:
        pinned = _pin_exact_owned_process(expected)
    except GpuLiveSmokeError as exc:
        if exc.code == "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH":
            _signal_trace_event(
                signal_trace,
                expected,
                sig,
                "UID_REJECTED",
                ownership="REJECTED_FOREIGN",
            )
        raise
    if pinned is None:
        _signal_trace_event(signal_trace, expected, sig, "EXITED_BEFORE_PIDFD_OPEN")
        return False
    pidfd, procdir_fd, _identity = pinned
    try:
        _signal_trace_event(signal_trace, expected, sig, "PIDFD_OPENED")
        _signal_trace_event(signal_trace, expected, sig, "IDENTITY_REVALIDATED")
        if not _pidfd_send_signal(pidfd, sig):
            _signal_trace_event(signal_trace, expected, sig, "EXITED_BEFORE_SIGNAL")
            return False
        _signal_trace_event(signal_trace, expected, sig, "SENT")
        return True
    finally:
        os.close(procdir_fd)
        os.close(pidfd)


def _deepest_first_members(
    members: tuple[ProcessIdentity, ...],
    recorded_tree: tuple[ProcessIdentity, ...],
) -> tuple[ProcessIdentity, ...]:
    """Order frozen descendants by ancestry depth, never by PID allocation order."""

    recorded = {item.pid: item for item in recorded_tree}
    depths: dict[int, int] = {}

    def depth(pid: int, visiting: set[int]) -> int:
        cached = depths.get(pid)
        if cached is not None:
            return cached
        if pid in visiting:
            _fail("GPU_SMOKE_PROCESS_TREE_INVALID", "recorded process ancestry contains a cycle")
        expected = recorded.get(pid)
        if expected is None:
            _fail("GPU_SMOKE_PROCESS_TREE_DRIFT", "live member is outside the frozen tree")
        parent = recorded.get(expected.ppid)
        result = 0 if parent is None else depth(parent.pid, {*visiting, pid}) + 1
        depths[pid] = result
        return result

    return tuple(sorted(members, key=lambda item: (-depth(item.pid, set()), item.pid)))


def _stop_owned_service_impl(
    guard: ProcessOwnershipGuard,
    process: subprocess.Popen[bytes],
    signal_trace: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    """Stop only exact revalidated members; never use killpg/pkill/killall."""

    if not guard.service_tree or all(
        item.pid != guard.root.pid or not _same_process(item, guard.root)
        for item in guard.service_tree
    ):
        _fail(
            "GPU_SMOKE_PROCESS_TREE_UNBOUND",
            "service tree was not recorded before cleanup; no process was signaled",
        )
    root_live = process.poll() is None
    members = _validate_owned_members(
        guard,
        require_root=root_live,
        allow_reparented_descendants=not root_live,
    )
    if _listening_socket_inodes():
        _assert_listener_owned_by_members(members)
    signaled_term: list[int] = []
    for member in _deepest_first_members(members, guard.service_tree):
        if _signal_exact_member(member, signal.SIGTERM, signal_trace):
            signaled_term.append(member.pid)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        remaining = _validate_owned_members(
            guard,
            require_root=False,
            allow_reparented_descendants=True,
        )
        if not remaining:
            break
        time.sleep(0.1)
    remaining = _validate_owned_members(
        guard,
        require_root=False,
        allow_reparented_descendants=True,
    )
    signaled_kill: list[int] = []
    for member in _deepest_first_members(remaining, guard.service_tree):
        if _signal_exact_member(member, signal.SIGKILL, signal_trace):
            signaled_kill.append(member.pid)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED",
            "guarded root did not exit after exact signals",
        ) from exc
    if _validate_owned_members(
        guard,
        require_root=False,
        allow_reparented_descendants=True,
    ):
        _fail("GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED", "guarded process session remains")
    _assert_port_free()
    return {
        "term_pids": signaled_term,
        "kill_pids": signaled_kill,
        "foreign_processes_signaled": 0,
        "broad_signal_used": False,
        "port_released": True,
        "process_group_released": True,
        "process_session_released": True,
        "signal_trace": cast(JsonValue, signal_trace),
    }


def _stop_owned_service(
    guard: ProcessOwnershipGuard,
    process: subprocess.Popen[bytes],
) -> dict[str, JsonValue]:
    signal_trace: list[dict[str, JsonValue]] = []
    try:
        return _stop_owned_service_impl(guard, process, signal_trace)
    except BaseException as exc:
        _raise_with_signal_trace(exc, signal_trace)


def _verify_scratch_directory(path: Path, *, create: bool) -> None:
    try:
        directory_fd = _open_secure_directory(path, create=create)
    except (OSError, GpuLiveSmokeError) as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
            "runtime scratch path is absent, linked, or not a directory",
        ) from exc
    try:
        metadata = os.fstat(directory_fd)
        if not (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700
        ):
            _fail(
                "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                "runtime scratch directory must be owner-only and owned by the namespace runner",
            )
    finally:
        os.close(directory_fd)


def _server_bootstrap_code(site_packages_path: str) -> str:
    site_literal = json.dumps(site_packages_path, ensure_ascii=True)
    return (
        "import runpy,sys;"
        f"sys.path.insert(0,{site_literal});"
        "sys.argv[0]='vllm';"
        "runpy.run_module('vllm.entrypoints.cli.main',run_name='__main__')"
    )


def _create_scratch_leaf(path: Path, *, exclusive: bool) -> None:
    parent_fd = _open_secure_directory(path.parent, create=False)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            if exclusive:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_RUNTIME_SCRATCH_COLLISION",
                    "runtime scratch run/model leaf already exists",
                ) from exc
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        child_fd = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            metadata = os.fstat(child_fd)
            if not (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o700
            ):
                _fail(
                    "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                    "runtime scratch leaf is not an owner-only directory",
                )
        finally:
            os.close(child_fd)
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
            "runtime scratch leaf could not be created without following links",
        ) from exc
    finally:
        os.close(parent_fd)


def prepare_network_namespace_launcher(authority: GpuLiveAuthority) -> dict[str, str]:
    """Prepare only owner-only launcher scratch and return the closed exec environment."""

    namespace = authority.network_namespace
    host_identity = (
        os.getuid() == namespace["host_owner_uid"]
        and os.getgid() == namespace["host_owner_gid"]
        and [os.getgid(), *sorted(group for group in os.getgroups() if group != os.getgid())]
        == _HOST_GROUP_VECTOR
        and sorted(os.getgroups()) == [109, 999, 1035]
    )
    inside_identity = (
        os.getuid() == namespace["inside_owner_uid"] == 0
        and os.getgid() == namespace["inside_owner_gid"] == 0
        and sorted(os.getgroups()) == _INSIDE_SUPPLEMENTARY_GIDS_SORTED
        and _normalized_id_map("/proc/self/uid_map") == namespace["uid_map_line"]
        and _normalized_id_map("/proc/self/gid_map") == namespace["gid_map_line"]
    )
    if not (host_identity or inside_identity):
        _fail(
            "GPU_SMOKE_AUTHORITY_OWNER_MISMATCH",
            "launcher scratch preparation is outside the authority-bound host/user namespace",
        )
    for prefix in ("env", "unshare", "ip", "setpriv"):
        _hash_regular_file(
            cast(str, namespace[f"{prefix}_path"]),
            expected_sha256=cast(str, namespace[f"{prefix}_sha256"]),
        )
    scratch_root = Path(authority.runtime_scratch_root)
    _verify_scratch_directory(scratch_root, create=True)
    launcher_root = scratch_root / "namespace-launcher"
    _create_scratch_leaf(launcher_root, exclusive=True)
    for name in _SERVER_SCRATCH_DIRECTORY_NAMES.values():
        _create_scratch_leaf(launcher_root / name, exclusive=True)
    environment = cast(dict[str, str], namespace["launcher_environment"])
    for key, name in _SERVER_SCRATCH_DIRECTORY_NAMES.items():
        if environment[key] != str(launcher_root / name):
            _fail(
                "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
                "launcher scratch environment differs from authority root",
            )
        _verify_scratch_directory(Path(environment[key]), create=False)
    return dict(environment)


def _scratch_census(path: Path, *, run_id: str, model_id: str, phase: str) -> dict[str, JsonValue]:
    _verify_scratch_directory(path, create=False)
    pending = [path]
    entries: list[dict[str, JsonValue]] = []
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                "runtime scratch census failed",
            ) from exc
        for child in children:
            member = Path(child.path)
            relative = member.relative_to(path).as_posix()
            try:
                before = member.lstat()
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                    "runtime scratch member disappeared",
                ) from exc
            if before.st_uid != os.getuid():
                _fail(
                    "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                    "runtime scratch member is not owned by the namespace runner",
                )
            if stat.S_ISDIR(before.st_mode):
                entries.append(
                    {
                        "path": relative,
                        "entry_type": "DIRECTORY",
                        "mode": stat.S_IMODE(before.st_mode),
                        "byte_count": 0,
                        "sha256": None,
                    }
                )
                pending.append(member)
                continue
            if not stat.S_ISREG(before.st_mode):
                _fail(
                    "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                    "runtime scratch contains a link or special file",
                )
            try:
                fd = os.open(member, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or _stat_identity(before) != _stat_identity(
                        opened
                    ):
                        _fail(
                            "GPU_SMOKE_RUNTIME_SCRATCH_CHANGED",
                            "runtime scratch file changed before hashing",
                        )
                    if opened.st_size > 8 * 1024 * 1024 * 1024:
                        _fail(
                            "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                            "one runtime scratch file exceeds the 8 GiB census bound",
                        )
                    digest = hashlib.sha256()
                    remaining = opened.st_size
                    while remaining:
                        chunk = os.read(fd, min(8 * 1024 * 1024, remaining))
                        if not chunk:
                            _fail(
                                "GPU_SMOKE_RUNTIME_SCRATCH_CHANGED",
                                "runtime scratch file truncated while hashing",
                            )
                        digest.update(chunk)
                        remaining -= len(chunk)
                    after = os.fstat(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                    "runtime scratch file could not be read without following links",
                ) from exc
            if _stat_identity(opened) != _stat_identity(after):
                _fail(
                    "GPU_SMOKE_RUNTIME_SCRATCH_CHANGED",
                    "runtime scratch file changed while hashing",
                )
            total_bytes += opened.st_size
            if total_bytes > 16 * 1024 * 1024 * 1024:
                _fail(
                    "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                    "runtime scratch census exceeds the 16 GiB total bound",
                )
            entries.append(
                {
                    "path": relative,
                    "entry_type": "REGULAR_FILE",
                    "mode": stat.S_IMODE(opened.st_mode),
                    "byte_count": opened.st_size,
                    "sha256": digest.hexdigest(),
                }
            )
    entries.sort(key=lambda item: cast(str, item["path"]))
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-runtime-scratch-census/v1",
        "run_id": run_id,
        "model_id": model_id,
        "phase": phase,
        "model_scratch_root": str(path),
        "entry_count": len(entries),
        "regular_file_byte_count": total_bytes,
        "entries_sha256": canonical_sha256(cast(JsonValue, entries)),
        "entries": cast(JsonValue, entries),
        "symlink_count": 0,
        "foreign_owner_entry_count": 0,
    }


def _prepare_runtime_scratch(authority: GpuLiveAuthority, run_id: str) -> _RuntimeScratch:
    if _ID_RE.fullmatch(run_id) is None:
        _fail("GPU_SMOKE_RUNTIME_SCRATCH_INVALID", "runtime scratch run ID is unsafe")
    root = Path(authority.runtime_scratch_root)
    _verify_scratch_directory(root, create=False)
    run_root = root / run_id
    _create_scratch_leaf(run_root, exclusive=True)
    model_directories: dict[str, str] = {}
    pre_censuses: dict[str, dict[str, JsonValue]] = {}
    for model_id in MODEL_ORDER:
        model_root = run_root / model_id
        _create_scratch_leaf(model_root, exclusive=True)
        for name in _SERVER_SCRATCH_DIRECTORY_NAMES.values():
            _create_scratch_leaf(model_root / name, exclusive=True)
        model_directories[model_id] = str(model_root)
        census = _scratch_census(model_root, run_id=run_id, model_id=model_id, phase="PRE")
        if not (
            census["entry_count"] == len(_SERVER_SCRATCH_DIRECTORY_NAMES)
            and census["regular_file_byte_count"] == 0
        ):
            _fail(
                "GPU_SMOKE_RUNTIME_SCRATCH_NOT_EMPTY",
                "per-run/model runtime scratch is not initially empty",
            )
        pre_censuses[model_id] = census
    return _RuntimeScratch(
        run_id=run_id,
        run_root=str(run_root),
        model_directories=model_directories,
        pre_censuses=pre_censuses,
    )


def _server_environment(
    authority: GpuLiveAuthority,
    model_scratch_root: str,
) -> dict[str, str]:
    scratch = Path(model_scratch_root)
    expected_parent = Path(authority.runtime_scratch_root)
    try:
        scratch.relative_to(expected_parent)
    except ValueError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
            "model scratch is outside the authority-bound root",
        ) from exc
    _verify_scratch_directory(scratch, create=False)
    launcher_environment = cast(
        dict[str, JsonValue], authority.network_namespace["launcher_environment"]
    )
    env: dict[str, str] = {
        "PATH": "/usr/bin:/bin",
        "CUDA_VISIBLE_DEVICES": cast(str, authority.gpu["uuid"]),
        "LD_LIBRARY_PATH": cast(str, launcher_environment["LD_LIBRARY_PATH"]),
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
    for key, name in _SERVER_SCRATCH_DIRECTORY_NAMES.items():
        directory = scratch / name
        _verify_scratch_directory(directory, create=False)
        env[key] = str(directory)
    return env


def _capture_minimal_direct_child(
    process: subprocess.Popen[bytes], owner_uid: int
) -> _MinimalDirectChildIdentity:
    last_error: GpuLiveSmokeError | None = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            handles = _open_owned_process_handles(process.pid, owner_uid)
            if handles is None:
                raise GpuLiveSmokeError(
                    "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                    "new server child has no pinned procdir",
                )
            pidfd, procdir_fd = handles
            try:
                identity = _minimal_identity_from_procdir(procdir_fd, process.pid)
                if _minimal_identity_from_procdir(procdir_fd, process.pid) != identity:
                    _fail(
                        "GPU_SMOKE_PROCESS_TREE_DRIFT",
                        "new direct-child minimal identity changed during capture",
                    )
            finally:
                os.close(procdir_fd)
                os.close(pidfd)
        except GpuLiveSmokeError as exc:
            last_error = exc
            if process.poll() is not None:
                _fail(
                    "GPU_SMOKE_SERVER_EXITED_DURING_GUARD_CAPTURE",
                    "new server child exited before its launch identity was captured",
                )
            time.sleep(0.01)
            continue
        if not (
            identity.uid == owner_uid == os.getuid()
            and identity.pid == process.pid
            and identity.ppid == os.getpid()
            and identity.pgid == identity.pid
            and identity.sid == identity.pid
        ):
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "new process is not the exact direct child/session root",
            )
        return identity
    raise GpuLiveSmokeError(
        "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
        "new direct-child identity could not be captured",
    ) from last_error


def _minimal_identity_dict(identity: _MinimalDirectChildIdentity) -> dict[str, JsonValue]:
    return {
        "uid": identity.uid,
        "pid": identity.pid,
        "ppid": identity.ppid,
        "pgid": identity.pgid,
        "sid": identity.sid,
        "starttime_ticks": identity.starttime_ticks,
    }


def _freeze_failed_launch_acquisition(
    authority: GpuLiveAuthority,
    handle: _FailedLaunchCleanupHandle,
) -> tuple[_FailedLaunchCleanupHandle, dict[str, JsonValue]]:
    """Freeze a minimal Popen-direct-child tree without promoting it to a service."""

    acquisition_pidfd = handle.acquisition_pidfd
    if acquisition_pidfd < 0:
        _fail(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "initial direct-child pidfd was not captured; numeric PID reacquisition is forbidden",
        )
    root_live = _pidfd_process_is_live(acquisition_pidfd)
    minimal = handle.root_minimal
    if minimal is None and root_live:
        minimal = _capture_minimal_direct_child(handle.process, authority.owner_uid)
    if minimal is None:
        _fail(
            "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
            "provisional acquisition lacks its direct-child identity",
        )
    root: ProcessIdentity | None = None
    if root_live:
        root = _read_owned_identity_matching_minimal(minimal)
        if root is None or not (
            root.uid == minimal.uid
            and root.pid == minimal.pid
            and root.ppid == minimal.ppid
            and root.pgid == minimal.pgid
            and root.sid == minimal.sid
            and root.starttime_ticks == minimal.starttime_ticks
        ):
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "provisional acquisition root is not the pinned direct child",
            )
    live_recorded_members: tuple[ProcessIdentity, ...]
    if handle.evidence_frozen:
        live_recorded_members = _current_recorded_process_tree(
            handle.recorded_tree,
            allow_reparented_descendants=True,
        )
        discovered = handle.recorded_tree
    elif root is not None:
        discovered = _discover_owned_process_tree(root)
        live_recorded_members = discovered
    else:
        if not handle.recorded_tree:
            _fail(
                "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                "provisional root exited before any exact process tree was captured",
            )
        recorded_roots = [item for item in handle.recorded_tree if item.pid == minimal.pid]
        if len(recorded_roots) != 1 or not (
            recorded_roots[0].uid == minimal.uid
            and recorded_roots[0].ppid == minimal.ppid
            and recorded_roots[0].pgid == minimal.pgid
            and recorded_roots[0].sid == minimal.sid
            and recorded_roots[0].starttime_ticks == minimal.starttime_ticks
        ):
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "root-exited acquisition tree is not bound to the captured direct child",
            )
        live_recorded_members = _current_recorded_process_tree(
            handle.recorded_tree,
            allow_reparented_descendants=True,
        )
        discovered = handle.recorded_tree
    previously_recorded = {item.pid: item for item in handle.recorded_tree}
    discovered_by_pid = {item.pid: item for item in discovered}
    if any(
        pid not in discovered_by_pid or not _same_process(discovered_by_pid[pid], expected)
        for pid, expected in previously_recorded.items()
    ):
        _fail(
            "GPU_SMOKE_PROCESS_TREE_DRIFT",
            "provisional acquisition changed before its evidence freeze",
        )
    frozen = replace(
        handle,
        acquisition_pidfd=acquisition_pidfd,
        root_minimal=minimal,
        recorded_tree=discovered,
        evidence_frozen=True,
    )
    receipt: dict[str, JsonValue] = {
        "schema_version": "mobileworld.g1.gpu-live-smoke-provisional-acquisition/v1",
        "model_id": handle.model_id,
        "popen_pid": handle.process.pid,
        "acquisition_pidfd_open": True,
        "pidfd_signal_policy": "PIDFD_ONLY_NO_OS_KILL_FALLBACK",
        "root_minimal": _minimal_identity_dict(minimal),
        "recorded_tree": [item.to_dict() for item in discovered],
        "recorded_tree_count": len(discovered),
        "live_recorded_member_count": len(live_recorded_members),
        "root_live_at_freeze": root_live,
        "root_exited_before_freeze": not root_live,
        "expected_argv": list(handle.expected_argv),
        "expected_argv_sha256": canonical_sha256(list(handle.expected_argv)),
        "expected_model_id": handle.model_id,
        "expected_gpu_uuid": handle.gpu_uuid,
        "expected_host": handle.host,
        "expected_port": handle.port,
        "environment_sha256": handle.environment_sha256,
        "direct_child_ppid": os.getpid(),
        "service_launched": False,
        "cleanup_eligible_scope": "FROZEN_DIRECT_CHILD_AND_RECORDED_OWNED_DESCENDANTS_ONLY",
    }
    return frozen, receipt


def _close_failed_launch_acquisition(handle: _FailedLaunchCleanupHandle) -> None:
    if handle.acquisition_pidfd >= 0:
        try:
            os.close(handle.acquisition_pidfd)
        except OSError as exc:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED",
                "provisional acquisition pidfd could not be closed",
            ) from exc


def _cleanup_failed_launch_impl(
    authority: GpuLiveAuthority,
    handle: _FailedLaunchCleanupHandle,
    signal_trace: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    """Clean only the exact direct child and its fully proven SID descendants."""

    process = handle.process
    root_minimal = handle.root_minimal
    recorded_tree = handle.recorded_tree
    if root_minimal is None or not recorded_tree:
        _fail(
            "GPU_SMOKE_PROCESS_TREE_UNBOUND",
            "provisional acquisition was not frozen before cleanup; no signal was sent",
        )
    roots = [item for item in recorded_tree if item.pid == root_minimal.pid]
    if len(roots) != 1:
        _fail(
            "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
            "failed-launch recorded root differs from the direct child",
        )
    root = roots[0]
    if not (
        root.uid == root_minimal.uid
        and root.pid == root_minimal.pid
        and root.ppid == root_minimal.ppid
        and root.pgid == root_minimal.pgid
        and root.sid == root_minimal.sid
        and root.starttime_ticks == root_minimal.starttime_ticks
    ):
        _fail(
            "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
            "failed-launch frozen root differs from its minimal direct-child identity",
        )
    members = _current_recorded_process_tree(
        recorded_tree,
        allow_reparented_descendants=True,
    )
    direct_child_already_exited = all(item.pid != root.pid for item in members)
    recorded = {item.pid: item for item in recorded_tree}
    term_pids: list[int] = []
    for member in _deepest_first_members(members, recorded_tree):
        if _signal_exact_member(member, signal.SIGTERM, signal_trace):
            term_pids.append(member.pid)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        remaining = _current_recorded_process_tree(
            recorded_tree,
            allow_reparented_descendants=True,
        )
        if not remaining:
            break
        time.sleep(0.1)
    remaining = _current_recorded_process_tree(
        recorded_tree,
        allow_reparented_descendants=True,
    )
    kill_pids: list[int] = []
    for member in remaining:
        expected = recorded.get(member.pid)
        if expected is None or not _same_process_except_ppid(member, expected):
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "failed-launch session changed after TERM; KILL was refused",
            )
    for member in _deepest_first_members(remaining, recorded_tree):
        if _signal_exact_member(member, signal.SIGKILL, signal_trace):
            kill_pids.append(member.pid)
    process.wait(timeout=10)
    if _current_recorded_process_tree(
        recorded_tree,
        allow_reparented_descendants=True,
    ):
        _fail(
            "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED",
            "failed-launch service session remains",
        )
    _assert_port_free()
    gpu_processes = _inspect_gpu_processes(authority, owned_pids=set(recorded))
    residual = [
        cast(int, row["pid"])
        for row in cast(list[dict[str, JsonValue]], gpu_processes["processes"])
        if cast(int, row["pid"]) in recorded
    ]
    if residual:
        _fail(
            "GPU_SMOKE_OWN_GPU_ALLOCATION_REMAINS",
            "failed-launch PIDs retain GPU allocation after cleanup",
        )
    return {
        "term_pids": term_pids,
        "kill_pids": kill_pids,
        "direct_child_already_exited": direct_child_already_exited,
        "foreign_processes_signaled": 0,
        "port_released": True,
        "process_session_released": True,
        "gpu_allocation_released": True,
        "signal_trace": cast(JsonValue, signal_trace),
    }


def _cleanup_failed_launch(
    authority: GpuLiveAuthority,
    handle: _FailedLaunchCleanupHandle,
) -> dict[str, JsonValue]:
    signal_trace: list[dict[str, JsonValue]] = []
    try:
        return _cleanup_failed_launch_impl(
            authority,
            handle,
            signal_trace,
        )
    except BaseException as exc:
        _raise_with_signal_trace(exc, signal_trace)


def _start_server(
    authority: GpuLiveAuthority,
    plan: VllmLaunchPlan,
    log_handle: BinaryIO,
    model_scratch_root: str,
    environment: dict[str, str],
) -> tuple[subprocess.Popen[bytes], ProcessOwnershipGuard]:
    _assert_port_free()
    server = cast(dict[str, JsonValue], authority.value["server_runtime"])
    bootstrap_code = _server_bootstrap_code(cast(str, server["site_packages_path"]))
    command = (
        cast(str, server["python_path"]),
        *_ISOLATED_PYTHON_FLAGS,
        "-c",
        bootstrap_code,
        *plan.argv[1:],
    )
    expected_environment = _server_environment(authority, model_scratch_root)
    if environment != expected_environment:
        _fail(
            "GPU_SMOKE_SERVER_ENVIRONMENT_INVALID",
            "server environment differs from the frozen scratch/offline allowlist",
        )
    _scan_for_secrets(cast(JsonValue, environment))
    environment_sha256 = canonical_sha256(cast(JsonValue, environment))
    scratch_cwd = Path(model_scratch_root)
    snapshot_path = Path(plan.snapshot_path)
    try:
        scratch_cwd.relative_to(Path(authority.runtime_scratch_root))
    except ValueError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
            "server cwd is outside the authority-bound model scratch root",
        ) from exc
    if (
        scratch_cwd == snapshot_path
        or scratch_cwd in snapshot_path.parents
        or snapshot_path in scratch_cwd.parents
    ):
        _fail(
            "GPU_SMOKE_PROTECTED_ROOT_OVERLAP",
            "server cwd overlaps its read-only model snapshot",
        )
    _verify_scratch_directory(scratch_cwd, create=False)
    _assert_authority_active(authority)
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=environment,
            cwd=str(scratch_cwd),
            close_fds=True,
            start_new_session=True,
            shell=False,
        )
    except OSError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_SERVER_START_FAILED", "guarded vLLM process could not start"
        ) from exc
    acquisition_pidfd = -1
    minimal: _MinimalDirectChildIdentity | None = None
    provisional_tree: tuple[ProcessIdentity, ...] = ()
    try:
        acquisition_pidfd = _pidfd_open(process.pid)
        if acquisition_pidfd < 0:
            _fail(
                "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                "new server child exited before its acquisition pidfd was pinned",
            )
        minimal = _capture_minimal_direct_child(process, authority.owner_uid)
        provisional_root = _read_owned_identity_matching_minimal(minimal)
        if provisional_root is None or not (
            provisional_root.pid == minimal.pid
            and provisional_root.ppid == minimal.ppid
            and provisional_root.pgid == minimal.pgid
            and provisional_root.sid == minimal.sid
            and provisional_root.starttime_ticks == minimal.starttime_ticks
        ):
            _fail(
                "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
                "direct-child full identity could not be provisionally recorded",
            )
        provisional_tree = _discover_owned_process_tree(provisional_root)
        server_python = cast(str, server["python_resolved_path"])
        guard = _capture_guard(
            process,
            expected_argv=command,
            model_id=plan.model_id,
            snapshot_path=plan.snapshot_path,
            served_name=cast(str, MODEL_IDENTITIES[plan.model_id]["served_name"]),
            owner_uid=authority.owner_uid,
            expected_executable_path=server_python,
            expected_executable_sha256=cast(str, server["python_sha256"]),
            environment_sha256=environment_sha256,
            expected_root=provisional_root,
        )
        if not any(_same_process(member, guard.root) for member in provisional_tree):
            _fail(
                "GPU_SMOKE_PROCESS_TREE_DRIFT",
                "captured service root differs from its provisional acquisition tree",
            )
        os.close(acquisition_pidfd)
        acquisition_pidfd = -1
    except BaseException as exc:
        error = GpuLiveSmokeError(
            "GPU_SMOKE_SERVER_GUARD_CAPTURE_FAILED",
            "server guard capture failed; the acquisition is retained for evidenced cleanup",
        )
        error.execution_detail = {
            "cause_code": (
                exc.code if isinstance(exc, GpuLiveSmokeError) else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
            ),
            "acquisition_pid": process.pid,
            "acquisition_pidfd_open": acquisition_pidfd >= 0,
            "minimal_identity_captured": minimal is not None,
            "recorded_tree_count": len(provisional_tree),
            "service_launched": False,
        }
        error.failed_launch_cleanup_handle = _FailedLaunchCleanupHandle(
            process=process,
            acquisition_pidfd=acquisition_pidfd,
            root_minimal=minimal,
            recorded_tree=provisional_tree,
            model_id=plan.model_id,
            expected_argv=command,
            gpu_uuid=cast(str, authority.gpu["uuid"]),
            host="127.0.0.1",
            port=18007,
            environment_sha256=environment_sha256,
            evidence_frozen=False,
        )
        raise error from exc
    return process, replace(guard, service_tree=provisional_tree)


def _loopback_get(path: str, *, timeout: float) -> tuple[int, bytes]:
    if path not in {"/health", "/v1/models"}:
        _fail("GPU_SMOKE_LOOPBACK_PATH_INVALID", "unregistered loopback path")
    connection = http.client.HTTPConnection("127.0.0.1", 18007, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Host": "127.0.0.1:18007"})
        response = connection.getresponse()
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            _fail("GPU_SMOKE_ENDPOINT_RESPONSE_TOO_LARGE", "loopback response is oversized")
        return response.status, body
    finally:
        connection.close()


def _wait_server_ready(
    authority: GpuLiveAuthority,
    process: subprocess.Popen[bytes],
    guard: ProcessOwnershipGuard,
) -> dict[str, JsonValue]:
    deadline = time.monotonic() + 600.0
    last_error = "not_attempted"
    while time.monotonic() < deadline:
        _assert_authority_active(authority)
        if process.poll() is not None:
            _fail("GPU_SMOKE_SERVER_EXITED_EARLY", "vLLM exited before readiness")
        try:
            _assert_authority_active(authority)
            status, body = _loopback_get("/health", timeout=2.0)
            if status == 200:
                members = _assert_listener_owned(guard, allow_tree_extension=True)
                _assert_authority_active(authority)
                models_status, models_body = _loopback_get("/v1/models", timeout=5.0)
                if models_status != 200:
                    _fail("GPU_SMOKE_SERVER_MODEL_LIST_FAILED", "model-list endpoint failed")
                try:
                    model_payload = json.loads(models_body)
                    served = [item.get("id") for item in model_payload.get("data", [])]
                except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
                    raise GpuLiveSmokeError(
                        "GPU_SMOKE_SERVER_MODEL_LIST_FAILED", "model-list response is invalid"
                    ) from exc
                if guard.served_name not in served:
                    _fail("GPU_SMOKE_SERVER_MODEL_MISMATCH", "served model name differs")
                return {
                    "health_status": status,
                    "health_body_sha256": _sha256(body),
                    "model_list_sha256": _sha256(models_body),
                    "served_model_name": guard.served_name,
                    "owned_process_count": len(members),
                    "listener_owned": True,
                }
            last_error = f"http_{status}"
        except (OSError, http.client.HTTPException) as exc:
            last_error = type(exc).__name__
        time.sleep(1.0)
    _fail("GPU_SMOKE_SERVER_READY_TIMEOUT", f"vLLM readiness timed out ({last_error})")
    raise AssertionError("unreachable")


def _response_envelope(raw: bytes) -> tuple[dict[str, JsonValue], str]:
    try:
        value = json.loads(raw, object_pairs_hook=_duplicate_rejecting_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROVIDER_RESPONSE_INVALID", "provider response is not strict JSON"
        ) from exc
    if type(value) is not dict:
        _fail("GPU_SMOKE_PROVIDER_RESPONSE_INVALID", "provider response root is not an object")
    envelope = cast(dict[str, JsonValue], value)
    choices = envelope.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        _fail("GPU_SMOKE_PROVIDER_RESPONSE_INVALID", "exactly one provider choice is required")
    message = cast(dict[str, JsonValue], cast(dict[str, JsonValue], choices[0]).get("message"))
    content = message.get("content") if type(message) is dict else None
    if type(content) is not str or not cast(str, content).strip():
        _fail("GPU_SMOKE_PROVIDER_RESPONSE_INVALID", "assistant content is empty")
    return envelope, cast(str, content)


def _invoke_openai_call(
    authority: GpuLiveAuthority,
    descriptor: OpenAIChatCallDescriptor,
) -> dict[str, JsonValue]:
    if not (
        descriptor.endpoint_origin == "http://127.0.0.1:18007"
        and descriptor.endpoint_path == "/v1/chat/completions"
        and descriptor.sdk_max_retries == 0
        and descriptor.stream is False
    ):
        _fail("GPU_SMOKE_PROVIDER_CALL_INVALID", "call descriptor left frozen loopback policy")
    try:
        import httpx  # type: ignore[import-not-found]
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError as exc:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_CLIENT_RUNTIME_MISMATCH", "frozen client dependencies are unavailable"
        ) from exc
    started = time.monotonic_ns()
    physical_request_count = 0
    request_observations: list[dict[str, JsonValue]] = []

    def observe_request(request: object) -> None:
        nonlocal physical_request_count
        _assert_authority_active(authority)
        url = getattr(request, "url", None)
        scheme = getattr(url, "scheme", None)
        host = getattr(url, "host", None)
        port = getattr(url, "port", None)
        path = getattr(url, "path", None)
        query = getattr(url, "query", None)
        method = getattr(request, "method", None)
        query_present = query not in {b"", ""}
        physical_request_count += 1
        allowed = (
            scheme == "http"
            and method == "POST"
            and host == "127.0.0.1"
            and port == 18007
            and path == "/v1/chat/completions"
            and not query_present
        )
        request_observations.append(
            {
                "ordinal": physical_request_count,
                "method": cast(JsonValue, method),
                "scheme": cast(JsonValue, scheme),
                "host": cast(JsonValue, host),
                "port": cast(JsonValue, port),
                "path": cast(JsonValue, path),
                "query_present": query_present,
                "query_recorded": False,
                "headers_recorded": False,
                "exact_loopback_allowed": allowed,
            }
        )
        if not allowed:
            _fail(
                "GPU_SMOKE_NON_LOOPBACK_REQUEST_FORBIDDEN",
                "SDK attempted a request outside the exact loopback endpoint",
            )
        if physical_request_count > 1:
            _fail(
                "GPU_SMOKE_CALL_ATTEMPT_INVALID",
                "SDK attempted more than one physical request for a logical call",
            )

    try:
        with httpx.Client(
            trust_env=False,
            timeout=descriptor.timeout_seconds,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            event_hooks={"request": [observe_request]},
        ) as http_client:
            client = OpenAI(
                api_key="EMPTY",
                base_url="http://127.0.0.1:18007/v1",
                max_retries=0,
                timeout=descriptor.timeout_seconds,
                http_client=http_client,
            )
            try:
                response = client.chat.completions.with_raw_response.create(**descriptor.kwargs)
                status_code = response.status_code
                raw = bytes(response.content)
                request_id = response.headers.get("x-request-id")
            finally:
                client.close()
    except Exception as exc:
        # This records one application-visible attempt.  The SDK has max_retries=0
        # and the harness never retries a failed logical call.
        error = (
            exc
            if isinstance(exc, GpuLiveSmokeError)
            else GpuLiveSmokeError(
                "GPU_SMOKE_PROVIDER_CALL_FAILED",
                f"single visible provider invocation failed ({type(exc).__name__})",
            )
        )
        error.application_visible_attempt_count = 1
        error.physical_request_count = physical_request_count
        error.physical_request_count_upper_bound = max(1, physical_request_count)
        error.execution_detail = {
            "request_observations": cast(JsonValue, request_observations),
            "non_loopback_connection_count": sum(
                item["exact_loopback_allowed"] is False for item in request_observations
            ),
        }
        if error is exc:
            raise error
        raise error from exc
    latency = time.monotonic_ns() - started
    if physical_request_count != 1:
        error = GpuLiveSmokeError(
            "GPU_SMOKE_CALL_ATTEMPT_INVALID",
            "SDK did not expose exactly one physical request",
        )
        error.application_visible_attempt_count = 1
        error.physical_request_count = physical_request_count
        error.physical_request_count_upper_bound = 1
        error.execution_detail = {
            "request_observations": cast(JsonValue, request_observations),
            "non_loopback_connection_count": 0,
        }
        raise error
    response_metadata: dict[str, JsonValue] = {
        "status_code": status_code,
        "request_id": request_id,
        "latency_ns": latency,
        "raw_response_sha256": _sha256(raw),
        "request_observations": cast(JsonValue, request_observations),
    }
    if status_code != 200:
        error = GpuLiveSmokeError(
            "GPU_SMOKE_PROVIDER_HTTP_FAILURE", f"provider returned HTTP {status_code}"
        )
        error.application_visible_attempt_count = 1
        error.physical_request_count = 1
        error.physical_request_count_upper_bound = 1
        error.received_raw_response = raw
        error.received_response_metadata = response_metadata
        error.execution_detail = {
            "request_observations": cast(JsonValue, request_observations),
            "non_loopback_connection_count": 0,
        }
        raise error
    try:
        envelope, content = _response_envelope(raw)
    except GpuLiveSmokeError as error:
        error.application_visible_attempt_count = 1
        error.physical_request_count = 1
        error.physical_request_count_upper_bound = 1
        error.received_raw_response = raw
        error.received_response_metadata = response_metadata
        error.execution_detail = {
            "request_observations": cast(JsonValue, request_observations),
            "non_loopback_connection_count": 0,
        }
        raise
    usage = envelope.get("usage")
    return {
        "status_code": status_code,
        "request_id": request_id,
        "latency_ns": latency,
        "raw_response": raw,
        "raw_response_sha256": _sha256(raw),
        "content": content,
        "content_sha256": _sha256(content.encode("utf-8")),
        "usage": cast(JsonValue, usage),
        "sdk_hidden_retries": 0,
        "application_visible_attempt_count": 1,
        "physical_request_count": physical_request_count,
        "request_observations": cast(JsonValue, request_observations),
    }


def _load_parser(model_id: str) -> ModuleType:
    module_name, relative_path, digest = _PARSER_FILES[model_id]
    repository_root = Path(__file__).resolve().parents[4]
    _hash_regular_file(str(repository_root / relative_path), expected_sha256=digest)
    return importlib.import_module(module_name)


def _parse_inert(model_id: str, content: str) -> dict[str, JsonValue]:
    module = _load_parser(model_id)
    parser = getattr(module, "parse_action_to_structure_output", None)
    if not callable(parser):
        _fail("GPU_SMOKE_HOST_PARSER_MISSING", "frozen host parser symbol is absent")
    parse_action = cast(Callable[[str], object], parser)
    try:
        parsed = parse_action(content)
        parsed_bytes = canonical_json_bytes(cast(JsonValue, parsed))
    except Exception as exc:
        return {
            "classification": "HOST_PARSE_FAILURE",
            "error_class": type(exc).__name__,
            "parsed_action": None,
            "generated_action_executed": False,
        }
    return {
        "classification": "HOST_PARSEABLE_INERT_ACTION",
        "error_class": None,
        "parsed_action": cast(JsonValue, json.loads(parsed_bytes)),
        "parsed_action_sha256": _sha256(parsed_bytes),
        "generated_action_executed": False,
    }


class GpuLiveSmokeOperations:
    """Injectable live boundary used by CPU tests and the authorized executor."""

    def verify_runtime_bindings(self, authority: GpuLiveAuthority) -> dict[str, JsonValue]:
        return _verify_runtime_bindings(authority)

    def verify_runtime_trees_post(
        self,
        authority: GpuLiveAuthority,
        *,
        phase: str,
    ) -> dict[str, JsonValue]:
        return _verify_private_runtime_trees(authority, phase=phase)

    def assert_authority_active(self, authority: GpuLiveAuthority) -> None:
        _assert_authority_active(authority)

    def verify_network_namespace(self, authority: GpuLiveAuthority) -> dict[str, JsonValue]:
        return _verify_network_namespace(authority)

    def prepare_runtime_scratch(self, authority: GpuLiveAuthority, run_id: str) -> _RuntimeScratch:
        return _prepare_runtime_scratch(authority, run_id)

    def inspect_runtime_scratch(
        self,
        authority: GpuLiveAuthority,
        run_id: str,
        model_id: str,
    ) -> dict[str, JsonValue]:
        return _scratch_census(
            Path(authority.runtime_scratch_root) / run_id / model_id,
            run_id=run_id,
            model_id=model_id,
            phase="POST",
        )

    def inspect_launcher_scratch(
        self,
        authority: GpuLiveAuthority,
        run_id: str,
        phase: str,
    ) -> dict[str, JsonValue]:
        if phase not in {"PASS_POSTFLIGHT", "FAIL_POSTFLIGHT"}:
            _fail("GPU_SMOKE_RUNTIME_SCRATCH_INVALID", "launcher census phase is invalid")
        return _scratch_census(
            Path(authority.runtime_scratch_root) / "namespace-launcher",
            run_id=run_id,
            model_id="LAUNCHER",
            phase=phase,
        )

    def verify_snapshot(
        self,
        authority: GpuLiveAuthority,
        receipt: LivePreparationReceipt,
        model_id: str,
    ) -> dict[str, JsonValue]:
        return _verify_local_snapshot(authority, receipt, model_id)

    def inspect_gpu(self, authority: GpuLiveAuthority) -> dict[str, JsonValue]:
        return _inspect_gpu(authority)

    def inspect_gpu_processes(
        self, authority: GpuLiveAuthority, *, owned_pids: set[int]
    ) -> dict[str, JsonValue]:
        return _inspect_gpu_processes(authority, owned_pids=owned_pids)

    def assert_gpu_isolation(
        self,
        baseline: dict[str, JsonValue],
        current: dict[str, JsonValue],
        *,
        owned_pids: set[int],
        require_owned_absent: bool,
    ) -> dict[str, JsonValue]:
        return _assert_gpu_service_isolation(
            baseline,
            current,
            owned_pids=owned_pids,
            require_owned_absent=require_owned_absent,
        )

    def assert_port_free(self) -> None:
        _assert_port_free()

    def validate_immediate_launch_preflight(
        self,
        authority: GpuLiveAuthority,
        baseline_gpu_processes: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Recheck shared-GPU and port safety immediately before Popen."""

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
            "schema_version": "mobileworld.g1.gpu-live-smoke-immediate-launch-preflight/v1",
            "capacity": capacity,
            "gpu_processes": gpu_processes,
            "gpu_isolation": isolation,
            "port_free": True,
            "authority_active_at_launch_boundary": True,
            "model_process_started": False,
        }

    def start_server(
        self,
        authority: GpuLiveAuthority,
        plan: VllmLaunchPlan,
        log_handle: BinaryIO,
        model_scratch_root: str,
        environment: dict[str, str],
    ) -> tuple[subprocess.Popen[bytes], ProcessOwnershipGuard]:
        return _start_server(
            authority,
            plan,
            log_handle,
            model_scratch_root,
            environment,
        )

    def server_environment(
        self,
        authority: GpuLiveAuthority,
        model_scratch_root: str,
    ) -> dict[str, str]:
        return _server_environment(authority, model_scratch_root)

    def bind_service_tree(self, guard: ProcessOwnershipGuard) -> ProcessOwnershipGuard:
        return _bind_service_tree(guard)

    def wait_server_ready(
        self,
        authority: GpuLiveAuthority,
        process: subprocess.Popen[bytes],
        guard: ProcessOwnershipGuard,
    ) -> dict[str, JsonValue]:
        return _wait_server_ready(authority, process, guard)

    def assert_listener_owned(self, guard: ProcessOwnershipGuard) -> tuple[ProcessIdentity, ...]:
        return _assert_listener_owned(guard)

    def inspect_owned_inet_sockets(self, guard: ProcessOwnershipGuard) -> dict[str, JsonValue]:
        return _inspect_owned_inet_sockets(guard)

    def invoke_call(
        self,
        authority: GpuLiveAuthority,
        descriptor: OpenAIChatCallDescriptor,
    ) -> dict[str, JsonValue]:
        return _invoke_openai_call(authority, descriptor)

    def parse_inert(self, model_id: str, content: str) -> dict[str, JsonValue]:
        return _parse_inert(model_id, content)

    def stop_service(
        self, guard: ProcessOwnershipGuard, process: subprocess.Popen[bytes]
    ) -> dict[str, JsonValue]:
        return _stop_owned_service(guard, process)

    def cleanup_failed_launch(
        self,
        authority: GpuLiveAuthority,
        handle: _FailedLaunchCleanupHandle,
    ) -> dict[str, JsonValue]:
        return _cleanup_failed_launch(authority, handle)

    def freeze_failed_launch_acquisition(
        self,
        authority: GpuLiveAuthority,
        handle: _FailedLaunchCleanupHandle,
    ) -> tuple[_FailedLaunchCleanupHandle, dict[str, JsonValue]]:
        return _freeze_failed_launch_acquisition(authority, handle)

    def close_failed_launch_acquisition(self, handle: _FailedLaunchCleanupHandle) -> None:
        _close_failed_launch_acquisition(handle)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_object_ref(store: _EvidenceStore, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    _scan_for_secrets(cast(JsonValue, value))
    materialized = _materialize_owned_command_receipts(store, value)
    if type(materialized) is not dict:
        _fail("GPU_SMOKE_EVIDENCE_TERMINAL_INVALID", "evidence JSON object is not an object")
    materialized_object = cast(dict[str, JsonValue], materialized)
    if value.get("schema_version") == _OWNED_COMMAND_RECEIPT_SCHEMA_VERSION:
        return materialized_object
    return store.object(canonical_json_bytes(materialized_object), "application/json")


def _read_log_bytes(handle: BinaryIO, *, maximum_bytes: int = 64 * 1024 * 1024) -> bytes:
    handle.flush()
    handle.seek(0)
    data = handle.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        _fail("GPU_SMOKE_SERVER_LOG_TOO_LARGE", "server log exceeded the evidence bound")
    return data


def _raise_captured(error: BaseException) -> None:
    if isinstance(error, GpuLiveSmokeError):
        raise error
    raise GpuLiveSmokeError(
        "GPU_SMOKE_UNCLASSIFIED_FAILURE",
        f"unclassified live-smoke failure ({type(error).__name__})",
    ) from error


def _execution_inputs(
    authority: GpuLiveAuthority,
    packet: GpuSmokePacket,
    model_config_manifest_path: str | os.PathLike[str],
) -> tuple[
    dict[str, JsonValue],
    LivePreparationReceipt,
    tuple[VllmLaunchPlan, ...],
    tuple[OpenAIChatCallDescriptor, ...],
]:
    preparation = prepare_gpu_live_smoke(authority, packet, model_config_manifest_path)
    receipt = load_live_preparation(model_config_manifest_path)
    launches = tuple(
        prepare_vllm_launch_plan(
            receipt,
            model_id,
            cast(str, cast(dict[str, JsonValue], authority.models[model_id])["snapshot_path"]),
        )
        for model_id in MODEL_ORDER
    )
    descriptors = tuple(
        prepare_openai_chat_call(
            receipt,
            call.model_id,
            cast(dict[str, JsonValue], call.value["application_request"]),
            call.seed,
        )
        for call in packet.calls
    )
    if not (
        preparation["call_descriptors_sha256"]
        == canonical_sha256([item.to_dict() for item in descriptors])
        and preparation["launch_plans_sha256"]
        == canonical_sha256([item.to_dict() for item in launches])
    ):
        _fail("GPU_SMOKE_PREPARATION_DRIFT", "execution inputs differ from pure preparation")
    return preparation, receipt, launches, descriptors


def _execute_authorized_gpu_live_smoke(
    authority: GpuLiveAuthority,
    packet: GpuSmokePacket,
    model_config_manifest_path: str | os.PathLike[str],
    *,
    operations: GpuLiveSmokeOperations,
    stage1_preexec_receipt: dict[str, JsonValue] | None = None,
    preimport_runtime_receipt: dict[str, JsonValue] | None = None,
    bootstrap_receipts_required: bool = False,
) -> dict[str, JsonValue]:
    preparation, receipt, launches, descriptors = _execution_inputs(
        authority, packet, model_config_manifest_path
    )
    run_id = f"g1gpu-{authority.sha256[:12]}-{time.time_ns():x}-{os.getpid()}"
    store = _EvidenceStore(authority.evidence_root, run_id)
    started_at = _utc_now()
    call_refs: list[JsonValue] = []
    lifecycle_refs: list[JsonValue] = []
    snapshot_pre_refs: list[JsonValue] = []
    snapshot_post_refs: list[JsonValue] = []
    snapshot_pre_by_model: dict[str, dict[str, JsonValue]] = {}
    snapshot_post_model_ids: set[str] = set()
    scratch_pre_refs: list[JsonValue] = []
    scratch_post_refs: list[JsonValue] = []
    scratch_post_model_ids: set[str] = set()
    started_model_ids: list[str] = []
    server_environment_refs: list[JsonValue] = []
    signal_targets: set[int] = set()
    signal_trace: list[dict[str, JsonValue]] = []
    signal_trace_consumed_error_ids: set[int] = set()
    network_observations: list[dict[str, JsonValue]] = []
    socket_observations: list[dict[str, JsonValue]] = []
    credential_scan_refs: list[JsonValue] = []
    launch_tree_pids: set[int] = set()
    logical_calls = 0
    physical_requests = 0
    server_log_capture_expected = 0
    server_log_capture_succeeded = 0
    current_process: subprocess.Popen[bytes] | None = None
    current_guard: ProcessOwnershipGuard | None = None
    current_cleanup_guard: ProcessOwnershipGuard | None = None
    current_failed_launch: _FailedLaunchCleanupHandle | None = None
    current_log: BinaryIO | None = None
    baseline_gpu_processes: dict[str, JsonValue] | None = None
    namespace_ref: dict[str, JsonValue] | None = None
    launcher_scratch_post_ref: dict[str, JsonValue] | None = None
    runtime_tree_post_ref: dict[str, JsonValue] | None = None
    runtime_scratch: _RuntimeScratch | None = None
    authority_ref = store.object(authority.canonical_bytes, "application/json")
    packet_ref = store.object(packet.canonical_bytes, "application/json")
    preparation_ref = _json_object_ref(store, preparation)
    if bootstrap_receipts_required and (
        stage1_preexec_receipt is None or preimport_runtime_receipt is None
    ):
        _fail(
            "GPU_SMOKE_RUNTIME_TREE_INVALID",
            "real execution lacks the stdlib-only Stage1/Stage2 runtime receipts",
        )
    stage1_preexec_ref = (
        _json_object_ref(store, stage1_preexec_receipt)
        if stage1_preexec_receipt is not None
        else None
    )
    preimport_runtime_ref = (
        _json_object_ref(store, preimport_runtime_receipt)
        if preimport_runtime_receipt is not None
        else None
    )
    store.event(
        "RUN_STARTED",
        {
            "decision_id": DECISION_ID,
            "authority": authority_ref,
            "smoke_packet": packet_ref,
            "preparation": preparation_ref,
            "stage1_preexec": cast(JsonValue, stage1_preexec_ref),
            "stage2_preimport": cast(JsonValue, preimport_runtime_ref),
            "started_at_utc": started_at,
            "model_order": list(MODEL_ORDER),
            "planned_call_count": 22,
        },
    )
    try:
        namespace_receipt = operations.verify_network_namespace(authority)
        namespace_ref = _json_object_ref(store, namespace_receipt)
        runtime_scratch = operations.prepare_runtime_scratch(authority, run_id)
        scratch_pre_refs = [
            _json_object_ref(store, runtime_scratch.pre_censuses[model_id])
            for model_id in MODEL_ORDER
        ]
        runtime = operations.verify_runtime_bindings(authority)
        runtime_ref = _json_object_ref(store, runtime)
        if stage1_preexec_receipt is not None and preimport_runtime_receipt is not None:
            stage1_tree = cast(
                dict[str, JsonValue],
                cast(
                    dict[str, JsonValue],
                    stage1_preexec_receipt["private_runtime_preexec"],
                )["private_runtime"],
            )
            preimport_tree = cast(
                dict[str, JsonValue], preimport_runtime_receipt["private_runtime"]
            )
            production_tree = cast(
                dict[str, JsonValue],
                cast(dict[str, JsonValue], runtime["runtime_tree_pre"])["private_runtime"],
            )
            preimport_source = cast(
                dict[str, JsonValue],
                cast(dict[str, JsonValue], preimport_runtime_receipt["source_closure"])[
                    "source_tree"
                ],
            )
            production_source = cast(
                dict[str, JsonValue],
                cast(dict[str, JsonValue], runtime["implementation"])["source_tree_pre"],
            )
            stage1_groups = cast(
                dict[str, JsonValue], stage1_preexec_receipt["supplementary_groups"]
            )
            stage2_groups = cast(
                dict[str, JsonValue], preimport_runtime_receipt["supplementary_groups"]
            )
            production_groups = cast(
                dict[str, JsonValue], namespace_receipt["supplementary_groups"]
            )
            if not (
                stage1_tree["tree_sha256"]
                == preimport_tree["tree_sha256"]
                == production_tree["tree_sha256"]
                == authority.value["private_runtime"]["tree_sha256"]
                and preimport_source["tree_sha256"]
                == production_source["tree_sha256"]
                == authority.source["source_tree_sha256"]
                and set(stage1_groups) == _SUPPLEMENTARY_GROUP_RUNTIME_KEYS
                and set(stage2_groups) == _SUPPLEMENTARY_GROUP_RUNTIME_KEYS
                and set(production_groups) == _SUPPLEMENTARY_GROUP_RUNTIME_KEYS
                and stage1_groups["phase"] == "STAGE1_PRE_SETPRIV"
                and stage2_groups == production_groups
                and stage2_groups["phase"] == "STAGE2_POST_SETPRIV"
                and stage1_groups["observed_inside_supplementary_gids_sorted"]
                == stage2_groups["observed_inside_supplementary_gids_sorted"]
                == _INSIDE_SUPPLEMENTARY_GIDS_SORTED
            ):
                _fail(
                    "GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH",
                    "Stage1, Stage2, and production runtime/source/group censuses differ",
                )
        operations.assert_authority_active(authority)
        baseline_device = operations.inspect_gpu(authority)
        baseline_device_ref = _json_object_ref(store, baseline_device)
        baseline_gpu_processes = operations.inspect_gpu_processes(authority, owned_pids=set())
        baseline_gpu_processes_ref = _json_object_ref(store, baseline_gpu_processes)
        operations.assert_port_free()
        store.event(
            "PREFLIGHT_VALIDATED",
            {
                "runtime": runtime_ref,
                "stage1_preexec": cast(JsonValue, stage1_preexec_ref),
                "stage2_preimport": cast(JsonValue, preimport_runtime_ref),
                "network_namespace": namespace_ref,
                "runtime_scratch_pre": scratch_pre_refs,
                "gpu_device": baseline_device_ref,
                "gpu_processes": baseline_gpu_processes_ref,
                "port_free": True,
                "foreign_process_target_count": 0,
            },
        )
        repeat_content: dict[tuple[str, int], str] = {}
        for model_ordinal, (model_id, launch) in enumerate(
            zip(MODEL_ORDER, launches, strict=True), start=1
        ):
            if (
                current_process is not None
                or current_guard is not None
                or current_cleanup_guard is not None
                or current_failed_launch is not None
            ):
                _fail(
                    "GPU_SMOKE_SERVICE_SEQUENTIALITY_FAILED",
                    "previous model lifecycle did not close before next launch",
                )
            operations.assert_authority_active(authority)
            capacity_before = operations.inspect_gpu(authority)
            gpu_processes_before = operations.inspect_gpu_processes(authority, owned_pids=set())
            isolation_before = operations.assert_gpu_isolation(
                baseline_gpu_processes,
                gpu_processes_before,
                owned_pids=set(),
                require_owned_absent=True,
            )
            operations.assert_port_free()
            snapshot_pre = operations.verify_snapshot(authority, receipt, model_id)
            snapshot_pre_by_model[model_id] = snapshot_pre
            snapshot_pre_ref = _json_object_ref(store, snapshot_pre)
            snapshot_pre_refs.append(snapshot_pre_ref)
            capacity_before_ref = _json_object_ref(store, capacity_before)
            gpu_before_ref = _json_object_ref(store, gpu_processes_before)
            store.event(
                "MODEL_PREFLIGHT_VALIDATED",
                {
                    "model_ordinal": model_ordinal,
                    "model_id": model_id,
                    "capacity": capacity_before_ref,
                    "gpu_processes": gpu_before_ref,
                    "gpu_isolation": isolation_before,
                    "snapshot_pre": snapshot_pre_ref,
                    "port_free": True,
                },
            )

            model_error: BaseException | None = None
            cleanup_error: BaseException | None = None
            readiness: dict[str, JsonValue] | None = None
            gpu_during: dict[str, JsonValue] | None = None
            gpu_after: dict[str, JsonValue] | None = None
            cleanup: dict[str, JsonValue] | None = None
            log_ref: dict[str, JsonValue] | None = None
            snapshot_post_ref: dict[str, JsonValue] | None = None
            service_release_proven = False
            failed_launch_release_proven = False
            service_tree_frozen = False
            scratch_post_ref: dict[str, JsonValue] | None = None
            server_environment_ref: dict[str, JsonValue] | None = None
            immediate_launch_preflight: dict[str, JsonValue] | None = None
            immediate_launch_preflight_ref: dict[str, JsonValue] | None = None
            model_signal_trace_start = len(signal_trace)
            lifecycle_started_at = _utc_now()
            current_log = tempfile.TemporaryFile(mode="w+b")
            server_log_capture_expected += 1
            started_model_ids.append(model_id)
            try:
                if runtime_scratch is None:
                    _fail(
                        "GPU_SMOKE_RUNTIME_SCRATCH_INVALID",
                        "runtime scratch was not frozen before model launch",
                    )
                model_scratch_root = runtime_scratch.model_directories[model_id]
                server_environment = operations.server_environment(
                    authority,
                    model_scratch_root,
                )
                _scan_for_secrets(cast(JsonValue, server_environment))
                server_environment_receipt: dict[str, JsonValue] = {
                    "schema_version": ("mobileworld.g1.gpu-live-smoke-server-environment/v1"),
                    "run_id": run_id,
                    "model_id": model_id,
                    "model_scratch_root": model_scratch_root,
                    "environment": cast(JsonValue, server_environment),
                    "environment_keys": sorted(server_environment),
                    "environment_sha256": canonical_sha256(cast(JsonValue, server_environment)),
                    "closed_allowlist": True,
                    "ambient_environment_inherited": False,
                    "credential_field_count": 0,
                }
                server_environment_ref = _json_object_ref(
                    store,
                    server_environment_receipt,
                )
                server_environment_refs.append(server_environment_ref)
                if baseline_gpu_processes is None:
                    _fail(
                        "GPU_SMOKE_GPU_PROCESS_BASELINE_INVALID",
                        "GPU process baseline is absent at the launch boundary",
                    )
                immediate_launch_preflight = operations.validate_immediate_launch_preflight(
                    authority,
                    baseline_gpu_processes,
                )
                current_process, current_guard = operations.start_server(
                    authority,
                    launch,
                    current_log,
                    model_scratch_root,
                    server_environment,
                )
                immediate_launch_preflight_ref = _json_object_ref(
                    store,
                    immediate_launch_preflight,
                )
                if (
                    current_guard.environment_sha256
                    != server_environment_receipt["environment_sha256"]
                ):
                    _fail(
                        "GPU_SMOKE_SERVER_ENVIRONMENT_INVALID",
                        "process guard does not bind the exact Popen environment receipt",
                    )
                if not current_guard.service_tree:
                    _fail(
                        "GPU_SMOKE_PROCESS_TREE_UNBOUND",
                        "server start did not return its provisional owned process tree",
                    )
                store.event(
                    "SERVICE_LAUNCHED",
                    {
                        "model_ordinal": model_ordinal,
                        "model_id": model_id,
                        "launch_plan_sha256": canonical_sha256(launch.to_dict()),
                        "guard": current_guard.to_dict(),
                        "server_environment": server_environment_ref,
                        "immediate_launch_preflight": immediate_launch_preflight_ref,
                        "service_tree_frozen": True,
                        "readiness_tree_extension_allowed": True,
                    },
                )
                current_cleanup_guard = current_guard
                readiness = operations.wait_server_ready(
                    authority,
                    current_process,
                    current_guard,
                )
                ready_guard = operations.bind_service_tree(current_guard)
                owned_pids = {item.pid for item in ready_guard.service_tree}
                launch_tree_pids.update(owned_pids)
                gpu_during = operations.inspect_gpu_processes(authority, owned_pids=owned_pids)
                operations.assert_gpu_isolation(
                    baseline_gpu_processes,
                    gpu_during,
                    owned_pids=owned_pids,
                    require_owned_absent=False,
                )
                during_pids = {
                    cast(int, row["pid"])
                    for row in cast(list[dict[str, JsonValue]], gpu_during["processes"])
                }
                if not during_pids.intersection(owned_pids):
                    _fail(
                        "GPU_SMOKE_OWN_GPU_ALLOCATION_UNPROVEN",
                        "ready service has no GPU allocation attributable to its recorded tree",
                    )
                readiness_ref = _json_object_ref(store, readiness)
                gpu_during_ref = _json_object_ref(store, gpu_during)
                store.event(
                    "SERVICE_READY",
                    {
                        "model_ordinal": model_ordinal,
                        "model_id": model_id,
                        "guard": ready_guard.to_dict(),
                        "readiness": readiness_ref,
                        "gpu_processes": gpu_during_ref,
                        "service_tree_frozen": True,
                    },
                )
                current_guard = ready_guard
                current_cleanup_guard = ready_guard
                service_tree_frozen = True
                _append_socket_observation(
                    socket_observations,
                    operations.inspect_owned_inet_sockets(current_guard),
                    model_id=model_id,
                    phase="SERVICE_READY",
                    call_id=None,
                )

                model_pairs = [
                    (call, descriptor)
                    for call, descriptor in zip(packet.calls, descriptors, strict=True)
                    if call.model_id == model_id
                ]
                if len(model_pairs) != 11:
                    _fail("GPU_SMOKE_MATRIX_INVALID", "model must own exactly 11 calls")
                for call, descriptor in model_pairs:
                    operations.assert_listener_owned(current_guard)
                    call_id = cast(str, call.value["call_id"])
                    application_request_ref = store.object(
                        call.application_request_bytes, "application/json"
                    )
                    transmitted_request_ref = store.object(
                        descriptor.kwargs_canonical_bytes, "application/json"
                    )
                    if not (
                        descriptor.application_request_sha256 == application_request_ref["sha256"]
                        and descriptor.kwargs_sha256 == transmitted_request_ref["sha256"]
                    ):
                        _fail(
                            "GPU_SMOKE_TRANSMITTED_REQUEST_DRIFT",
                            "final transmitted request differs from frozen descriptor",
                        )
                    _append_socket_observation(
                        socket_observations,
                        operations.inspect_owned_inet_sockets(current_guard),
                        model_id=model_id,
                        phase="BEFORE_CALL",
                        call_id=call_id,
                    )
                    operations.assert_authority_active(authority)
                    try:
                        invocation = operations.invoke_call(authority, descriptor)
                    except BaseException as exc:
                        visible_attempts = (
                            exc.application_visible_attempt_count
                            if isinstance(exc, GpuLiveSmokeError)
                            else None
                        )
                        exact_physical = (
                            exc.physical_request_count
                            if isinstance(exc, GpuLiveSmokeError)
                            else None
                        )
                        upper_physical = (
                            exc.physical_request_count_upper_bound
                            if isinstance(exc, GpuLiveSmokeError)
                            else 1
                        )
                        if visible_attempts == 1:
                            logical_calls += 1
                        if exact_physical is not None:
                            physical_requests += exact_physical
                        received_raw = (
                            exc.received_raw_response
                            if isinstance(exc, GpuLiveSmokeError)
                            else None
                        )
                        received_metadata = (
                            exc.received_response_metadata
                            if isinstance(exc, GpuLiveSmokeError)
                            else None
                        )
                        credential_scan_ref: dict[str, JsonValue] | None = None
                        credential_match = False
                        if received_raw is not None:
                            credential_scan = _credential_scan_receipt(
                                received_raw,
                                artifact_kind="RAW_PROVIDER_RESPONSE",
                            )
                            credential_scan_ref = _json_object_ref(store, credential_scan)
                            credential_scan_refs.append(credential_scan_ref)
                            credential_match = credential_scan["match_count"] != 0
                        received_ref = (
                            store.object(received_raw, "application/json")
                            if received_raw is not None and not credential_match
                            else None
                        )
                        failure_call: dict[str, JsonValue] = {
                            "schema_version": "mobileworld.g1.gpu-live-smoke-call/v1",
                            "run_id": run_id,
                            "call_id": call_id,
                            "ordinal": len(call_refs) + 1,
                            "model_id": model_id,
                            "phase": call.phase,
                            "seed": call.seed,
                            "repeat_index": cast(JsonValue, call.value["repeat_index"]),
                            "arm": cast(JsonValue, call.value["arm"]),
                            "status": "FAIL",
                            "failure_stage": (
                                "POST_RESPONSE" if received_raw is not None else "PRE_RESPONSE"
                            ),
                            "application_request": application_request_ref,
                            "transmitted_request": transmitted_request_ref,
                            "raw_response": cast(JsonValue, received_ref),
                            "application_visible_attempt_count": cast(JsonValue, visible_attempts),
                            "physical_request_count": cast(JsonValue, exact_physical),
                            "physical_request_count_upper_bound": cast(JsonValue, upper_physical),
                            "sdk_hidden_retry_count": 0,
                            "error_code": (
                                "GPU_SMOKE_SECRET_FIELD_FORBIDDEN"
                                if credential_match
                                else exc.code
                                if isinstance(exc, GpuLiveSmokeError)
                                else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                            ),
                            "host_parser": None,
                            "response_feedback_used": False,
                            "generated_action_executed": False,
                        }
                        if received_raw is not None:
                            failure_call["response"] = cast(JsonValue, received_metadata)
                            failure_call["independent_client_state"] = True
                        if credential_match and received_raw is not None:
                            failure_call.update(
                                {
                                    "raw_response_sha256": _sha256(received_raw),
                                    "raw_response_byte_count": len(received_raw),
                                    "credential_scan": cast(JsonValue, credential_scan_ref),
                                    "secret_material_persisted": False,
                                }
                            )
                        failure_ref = _json_object_ref(store, failure_call)
                        call_refs.append(failure_ref)
                        store.event(
                            "CALL_FAILED",
                            {"call_id": call_id, "call_receipt": failure_ref},
                        )
                        if isinstance(exc, GpuLiveSmokeError):
                            _extend_network_observations(
                                network_observations,
                                exc.execution_detail,
                            )
                        _append_socket_observation(
                            socket_observations,
                            operations.inspect_owned_inet_sockets(current_guard),
                            model_id=model_id,
                            phase="AFTER_FAILED_CALL",
                            call_id=call_id,
                        )
                        if credential_match:
                            raise GpuLiveSmokeError(
                                "GPU_SMOKE_SECRET_FIELD_FORBIDDEN",
                                "raw provider response matched a forbidden credential pattern",
                            )
                        raise
                    raw_response = invocation.pop("raw_response", None)
                    content = invocation.pop("content", None)
                    if type(raw_response) is not bytes or type(content) is not str:
                        _fail(
                            "GPU_SMOKE_PROVIDER_RESPONSE_INVALID",
                            "provider invocation omitted raw bytes or content",
                        )
                    visible_attempts = invocation.get("application_visible_attempt_count")
                    exact_physical = invocation.get("physical_request_count")
                    retries = invocation.get("sdk_hidden_retries")
                    if visible_attempts == 1:
                        logical_calls += 1
                    if type(exact_physical) is int:
                        physical_requests += exact_physical
                    credential_scan = _credential_scan_receipt(
                        cast(bytes, raw_response),
                        artifact_kind="RAW_PROVIDER_RESPONSE",
                    )
                    credential_scan_ref = _json_object_ref(store, credential_scan)
                    credential_scan_refs.append(credential_scan_ref)
                    if credential_scan["match_count"] != 0:
                        credential_failure: dict[str, JsonValue] = {
                            "schema_version": "mobileworld.g1.gpu-live-smoke-call/v1",
                            "run_id": run_id,
                            "call_id": call_id,
                            "ordinal": len(call_refs) + 1,
                            "model_id": model_id,
                            "phase": call.phase,
                            "seed": call.seed,
                            "repeat_index": cast(JsonValue, call.value["repeat_index"]),
                            "arm": cast(JsonValue, call.value["arm"]),
                            "status": "FAIL",
                            "failure_stage": "POST_RESPONSE",
                            "application_request": application_request_ref,
                            "transmitted_request": transmitted_request_ref,
                            "raw_response": None,
                            "raw_response_sha256": _sha256(cast(bytes, raw_response)),
                            "raw_response_byte_count": len(cast(bytes, raw_response)),
                            "credential_scan": credential_scan_ref,
                            "secret_material_persisted": False,
                            "response": cast(JsonValue, invocation),
                            "application_visible_attempt_count": cast(JsonValue, visible_attempts),
                            "physical_request_count": cast(JsonValue, exact_physical),
                            "physical_request_count_upper_bound": 1,
                            "sdk_hidden_retry_count": cast(JsonValue, retries),
                            "error_code": "GPU_SMOKE_SECRET_FIELD_FORBIDDEN",
                            "host_parser": None,
                            "independent_client_state": True,
                            "response_feedback_used": False,
                            "generated_action_executed": False,
                        }
                        credential_failure_ref = _json_object_ref(store, credential_failure)
                        call_refs.append(credential_failure_ref)
                        store.event(
                            "CALL_FAILED",
                            {
                                "call_id": call_id,
                                "call_receipt": credential_failure_ref,
                            },
                        )
                        _extend_network_observations(network_observations, invocation)
                        _append_socket_observation(
                            socket_observations,
                            operations.inspect_owned_inet_sockets(current_guard),
                            model_id=model_id,
                            phase="AFTER_CALL",
                            call_id=call_id,
                        )
                        raise GpuLiveSmokeError(
                            "GPU_SMOKE_SECRET_FIELD_FORBIDDEN",
                            "raw provider response matched a forbidden credential pattern",
                        )
                    raw_response_ref = store.object(cast(bytes, raw_response), "application/json")
                    try:
                        _extend_network_observations(network_observations, invocation)
                        _append_socket_observation(
                            socket_observations,
                            operations.inspect_owned_inet_sockets(current_guard),
                            model_id=model_id,
                            phase="AFTER_CALL",
                            call_id=call_id,
                        )
                    except BaseException as post_call_census_error:
                        census_failure: dict[str, JsonValue] = {
                            "schema_version": "mobileworld.g1.gpu-live-smoke-call/v1",
                            "run_id": run_id,
                            "call_id": call_id,
                            "ordinal": len(call_refs) + 1,
                            "model_id": model_id,
                            "phase": call.phase,
                            "seed": call.seed,
                            "repeat_index": cast(JsonValue, call.value["repeat_index"]),
                            "arm": cast(JsonValue, call.value["arm"]),
                            "status": "FAIL",
                            "failure_stage": "POST_RESPONSE",
                            "application_request": application_request_ref,
                            "transmitted_request": transmitted_request_ref,
                            "raw_response": raw_response_ref,
                            "response": cast(JsonValue, invocation),
                            "application_visible_attempt_count": cast(JsonValue, visible_attempts),
                            "physical_request_count": cast(JsonValue, exact_physical),
                            "physical_request_count_upper_bound": 1,
                            "sdk_hidden_retry_count": cast(JsonValue, retries),
                            "error_code": (
                                post_call_census_error.code
                                if isinstance(post_call_census_error, GpuLiveSmokeError)
                                else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                            ),
                            "host_parser": None,
                            "independent_client_state": True,
                            "response_feedback_used": False,
                            "generated_action_executed": False,
                        }
                        census_failure_ref = _json_object_ref(store, census_failure)
                        call_refs.append(census_failure_ref)
                        store.event(
                            "CALL_FAILED",
                            {
                                "call_id": call_id,
                                "call_receipt": census_failure_ref,
                            },
                        )
                        raise
                    post_response_error: GpuLiveSmokeError | None = None
                    parser: dict[str, JsonValue] | None = None
                    if visible_attempts != 1 or exact_physical != 1 or retries != 0:
                        post_response_error = GpuLiveSmokeError(
                            "GPU_SMOKE_CALL_ATTEMPT_INVALID",
                            "call did not use exactly one visible attempt and zero hidden retries",
                        )
                    else:
                        try:
                            parser = operations.parse_inert(model_id, cast(str, content))
                        except BaseException as exc:
                            parser = {
                                "classification": "HOST_PARSE_EXCEPTION",
                                "error_class": type(exc).__name__,
                                "parsed_action": None,
                                "generated_action_executed": False,
                            }
                            post_response_error = GpuLiveSmokeError(
                                "GPU_SMOKE_HOST_PARSE_FAILED",
                                "frozen host parser raised while parsing returned text",
                            )
                        if parser.get("classification") != "HOST_PARSEABLE_INERT_ACTION":
                            post_response_error = GpuLiveSmokeError(
                                "GPU_SMOKE_HOST_PARSE_FAILED",
                                "returned text is not parseable by the frozen host parser",
                            )
                    if post_response_error is None and call.phase == "G1_4_CANARY":
                        key = (model_id, call.seed)
                        content_sha = cast(str, invocation["content_sha256"])
                        if (
                            call.value["repeat_index"] == 2
                            and repeat_content.get(key) != content_sha
                        ):
                            post_response_error = GpuLiveSmokeError(
                                "GPU_SMOKE_CANARY_REPEAT_MISMATCH",
                                "fresh repeat content differs for the same model/seed",
                            )
                    if post_response_error is not None:
                        post_failure: dict[str, JsonValue] = {
                            "schema_version": "mobileworld.g1.gpu-live-smoke-call/v1",
                            "run_id": run_id,
                            "call_id": call_id,
                            "ordinal": len(call_refs) + 1,
                            "model_id": model_id,
                            "phase": call.phase,
                            "seed": call.seed,
                            "repeat_index": cast(JsonValue, call.value["repeat_index"]),
                            "arm": cast(JsonValue, call.value["arm"]),
                            "status": "FAIL",
                            "failure_stage": "POST_RESPONSE",
                            "application_request": application_request_ref,
                            "transmitted_request": transmitted_request_ref,
                            "raw_response": raw_response_ref,
                            "response": cast(JsonValue, invocation),
                            "application_visible_attempt_count": cast(JsonValue, visible_attempts),
                            "physical_request_count": cast(JsonValue, exact_physical),
                            "physical_request_count_upper_bound": 1,
                            "sdk_hidden_retry_count": 0,
                            "error_code": post_response_error.code,
                            "host_parser": cast(JsonValue, parser),
                            "independent_client_state": True,
                            "response_feedback_used": False,
                            "generated_action_executed": False,
                        }
                        failure_ref = _json_object_ref(store, post_failure)
                        call_refs.append(failure_ref)
                        store.event(
                            "CALL_FAILED",
                            {"call_id": call_id, "call_receipt": failure_ref},
                        )
                        raise post_response_error
                    if parser is None:
                        _fail(
                            "GPU_SMOKE_HOST_PARSE_FAILED",
                            "host parser did not return a result",
                        )
                    call_receipt: dict[str, JsonValue] = {
                        "schema_version": "mobileworld.g1.gpu-live-smoke-call/v1",
                        "run_id": run_id,
                        "call_id": call_id,
                        "ordinal": len(call_refs) + 1,
                        "model_id": model_id,
                        "phase": call.phase,
                        "seed": call.seed,
                        "repeat_index": cast(JsonValue, call.value["repeat_index"]),
                        "arm": cast(JsonValue, call.value["arm"]),
                        "status": "PASS",
                        "application_request": application_request_ref,
                        "transmitted_request": transmitted_request_ref,
                        "render_evidence": cast(JsonValue, call.value["render_evidence"]),
                        "raw_response": raw_response_ref,
                        "response": cast(JsonValue, invocation),
                        "host_parser": parser,
                        "physical_request_count": 1,
                        "sdk_hidden_retry_count": 0,
                        "independent_client_state": True,
                        "response_feedback_used": False,
                        "generated_action_executed": False,
                    }
                    call_ref = _json_object_ref(store, call_receipt)
                    call_refs.append(call_ref)
                    store.event(
                        "CALL_COMPLETED",
                        {
                            "call_id": call_id,
                            "ordinal": logical_calls,
                            "call_receipt": call_ref,
                        },
                    )
                    if call.phase == "G1_4_CANARY":
                        key = (model_id, call.seed)
                        content_sha = cast(str, invocation["content_sha256"])
                        if call.value["repeat_index"] == 1:
                            repeat_content[key] = content_sha
            except BaseException as exc:
                if isinstance(exc, GpuLiveSmokeError) and isinstance(
                    exc.failed_launch_cleanup_handle,
                    _FailedLaunchCleanupHandle,
                ):
                    current_failed_launch = exc.failed_launch_cleanup_handle
                if isinstance(exc, GpuLiveSmokeError) and isinstance(exc.execution_detail, dict):
                    detail = cast(dict[str, JsonValue], exc.execution_detail)
                    _extend_signal_trace(
                        signal_trace,
                        detail,
                        cleanup_attempt=f"model-{model_ordinal}-failed-launch",
                    )
                    signal_trace_consumed_error_ids.add(id(exc))
                    failed_launch_targets = {
                        cast(int, pid)
                        for key in ("term_pids", "kill_pids")
                        for pid in cast(list[JsonValue], detail.get(key, []))
                    }
                    signal_targets.update(failed_launch_targets)
                    launch_tree_pids.update(failed_launch_targets)
                model_error = exc
            finally:
                if (
                    immediate_launch_preflight is not None
                    and immediate_launch_preflight_ref is None
                ):
                    immediate_launch_preflight_ref = _json_object_ref(
                        store,
                        immediate_launch_preflight,
                    )
                if current_failed_launch is not None:
                    try:
                        current_failed_launch, acquisition_receipt = (
                            operations.freeze_failed_launch_acquisition(
                                authority,
                                current_failed_launch,
                            )
                        )
                        acquisition_ref = _json_object_ref(store, acquisition_receipt)
                        provisional_pids = {
                            item.pid for item in current_failed_launch.recorded_tree
                        }
                        launch_tree_pids.update(provisional_pids)
                        store.event(
                            "PROVISIONAL_ACQUISITION_FROZEN",
                            {
                                "model_ordinal": model_ordinal,
                                "model_id": model_id,
                                "acquisition": acquisition_ref,
                                "cleanup_eligible_pids": sorted(provisional_pids),
                                "service_launched": False,
                            },
                        )
                        failed_launch_cleanup = operations.cleanup_failed_launch(
                            authority,
                            current_failed_launch,
                        )
                        _extend_signal_trace(
                            signal_trace,
                            failed_launch_cleanup,
                            cleanup_attempt=f"model-{model_ordinal}-provisional",
                        )
                        failed_launch_signaled = {
                            cast(int, pid)
                            for key in ("term_pids", "kill_pids")
                            for pid in cast(list[JsonValue], failed_launch_cleanup.get(key, []))
                        }
                        if not failed_launch_signaled.issubset(provisional_pids):
                            _fail(
                                "GPU_SMOKE_FOREIGN_PROCESS_TARGET_ATTEMPT",
                                "provisional cleanup target is outside the frozen acquisition",
                            )
                        signal_targets.update(failed_launch_signaled)
                        operations.close_failed_launch_acquisition(current_failed_launch)
                        store.event(
                            "PROVISIONAL_ACQUISITION_CLEANUP",
                            {
                                "model_ordinal": model_ordinal,
                                "model_id": model_id,
                                "acquisition": acquisition_ref,
                                "cleanup": failed_launch_cleanup,
                                "service_launched": False,
                                "release_proven": True,
                            },
                        )
                        current_failed_launch = None
                        failed_launch_release_proven = True
                    except BaseException as exc:
                        if isinstance(exc, GpuLiveSmokeError):
                            _extend_signal_trace(
                                signal_trace,
                                exc.execution_detail,
                                cleanup_attempt=f"model-{model_ordinal}-provisional-failed",
                            )
                            signal_trace_consumed_error_ids.add(id(exc))
                        store.event(
                            "SERVICE_CLEANUP_ATTEMPT_FAILED",
                            {
                                "model_id": model_id,
                                "failed_launch": True,
                                "error_code": (
                                    exc.code
                                    if isinstance(exc, GpuLiveSmokeError)
                                    else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                                ),
                                "signal_trace": cast(
                                    JsonValue,
                                    _signal_trace_from_value(
                                        exc.execution_detail
                                        if isinstance(exc, GpuLiveSmokeError)
                                        else None
                                    ),
                                ),
                            },
                        )
                        cleanup_error = exc
                if current_process is not None and current_guard is not None:
                    try:
                        if not service_tree_frozen:
                            cleanup_candidate = current_guard
                            if current_process.poll() is None:
                                cleanup_candidate = operations.bind_service_tree(current_guard)
                            if not cleanup_candidate.service_tree:
                                _fail(
                                    "GPU_SMOKE_PROCESS_TREE_UNBOUND",
                                    "failed-start cleanup tree is empty",
                                )
                            store.event(
                                "SERVICE_TREE_FROZEN_FOR_FAILED_START_CLEANUP",
                                {
                                    "model_id": model_id,
                                    "guard": cleanup_candidate.to_dict(),
                                    "service_tree_frozen": True,
                                    "root_already_exited": current_process.poll() is not None,
                                },
                            )
                            current_guard = cleanup_candidate
                            current_cleanup_guard = cleanup_candidate
                            service_tree_frozen = True
                        if current_cleanup_guard is None:
                            _fail(
                                "GPU_SMOKE_PROCESS_TREE_UNBOUND",
                                "no persistently frozen cleanup guard exists; signal refused",
                            )
                        owned_pids = {item.pid for item in current_cleanup_guard.service_tree}
                        launch_tree_pids.update(owned_pids)
                        cleanup = operations.stop_service(
                            current_cleanup_guard,
                            current_process,
                        )
                        _extend_signal_trace(
                            signal_trace,
                            cleanup,
                            cleanup_attempt=f"model-{model_ordinal}-primary",
                        )
                        signaled = {
                            cast(int, pid)
                            for key in ("term_pids", "kill_pids")
                            for pid in cast(list[JsonValue], cleanup.get(key, []))
                        }
                        if not signaled.issubset(owned_pids):
                            _fail(
                                "GPU_SMOKE_FOREIGN_PROCESS_TARGET_ATTEMPT",
                                "cleanup target is outside the recorded service tree",
                            )
                        signal_targets.update(signaled)
                        gpu_after = operations.inspect_gpu_processes(
                            authority, owned_pids=owned_pids
                        )
                        gpu_release = operations.assert_gpu_isolation(
                            baseline_gpu_processes,
                            gpu_after,
                            owned_pids=owned_pids,
                            require_owned_absent=True,
                        )
                        operations.assert_port_free()
                        # Retire the exact process guard only after all three
                        # closure facts are proven: guarded stop, no owned GPU
                        # PID, and released loopback port.  Snapshot validation
                        # is separate evidence and cannot make an exited PID a
                        # cleanup target again.
                        service_release_proven = True
                        snapshot_post = operations.verify_snapshot(authority, receipt, model_id)
                        if canonical_json_bytes(snapshot_pre) != canonical_json_bytes(
                            snapshot_post
                        ):
                            _fail(
                                "GPU_SMOKE_SNAPSHOT_PRE_POST_MISMATCH",
                                "model snapshot tree changed across service lifecycle",
                            )
                        snapshot_post_ref = _json_object_ref(store, snapshot_post)
                        snapshot_post_refs.append(snapshot_post_ref)
                        snapshot_post_model_ids.add(model_id)
                        cleanup["gpu_release"] = gpu_release
                        cleanup["snapshot_pre_post_identical"] = True
                    except BaseException as exc:
                        if isinstance(exc, GpuLiveSmokeError):
                            _extend_signal_trace(
                                signal_trace,
                                exc.execution_detail,
                                cleanup_attempt=f"model-{model_ordinal}-primary-failed",
                            )
                            signal_trace_consumed_error_ids.add(id(exc))
                        store.event(
                            "SERVICE_CLEANUP_ATTEMPT_FAILED",
                            {
                                "model_id": model_id,
                                "error_code": (
                                    exc.code
                                    if isinstance(exc, GpuLiveSmokeError)
                                    else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                                ),
                                "signal_trace": cast(
                                    JsonValue,
                                    _signal_trace_from_value(
                                        exc.execution_detail
                                        if isinstance(exc, GpuLiveSmokeError)
                                        else None
                                    ),
                                ),
                            },
                        )
                        cleanup_error = exc
                if runtime_scratch is not None and (
                    service_release_proven or failed_launch_release_proven
                ):
                    try:
                        scratch_post = operations.inspect_runtime_scratch(
                            authority,
                            run_id,
                            model_id,
                        )
                        scratch_post_ref = _json_object_ref(store, scratch_post)
                        scratch_post_refs.append(scratch_post_ref)
                        scratch_post_model_ids.add(model_id)
                    except BaseException as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                if (
                    current_log is not None
                    and current_failed_launch is None
                    and not (
                        current_process is not None
                        and current_guard is not None
                        and not service_release_proven
                    )
                ):
                    try:
                        log_bytes = _read_log_bytes(current_log)
                        credential_scan_refs.append(
                            _json_object_ref(
                                store,
                                _scan_bytes_for_credentials(
                                    log_bytes,
                                    artifact_kind="SERVER_LOG",
                                ),
                            )
                        )
                        log_ref = store.object(log_bytes, "text/plain; charset=utf-8")
                        server_log_capture_succeeded += 1
                    except BaseException as exc:
                        store.event(
                            "SERVER_LOG_CAPTURE_FAILED",
                            {
                                "error_code": (
                                    exc.code
                                    if isinstance(exc, GpuLiveSmokeError)
                                    else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                                )
                            },
                        )
                        if cleanup_error is None:
                            cleanup_error = exc
                    finally:
                        current_log.close()
                        current_log = None
                if current_guard is not None:
                    lifecycle_signal_summary = _signal_trace_summary(
                        signal_trace[model_signal_trace_start:]
                    )
                    lifecycle: dict[str, JsonValue] = {
                        "schema_version": "mobileworld.g1.gpu-live-smoke-lifecycle/v1",
                        "run_id": run_id,
                        "model_ordinal": model_ordinal,
                        "model_id": model_id,
                        "started_at_utc": lifecycle_started_at,
                        "finished_at_utc": _utc_now(),
                        "status": "PASS"
                        if model_error is None and cleanup_error is None
                        else "FAIL",
                        "launch_plan_sha256": canonical_sha256(launch.to_dict()),
                        "guard": current_guard.to_dict(),
                        "readiness": cast(JsonValue, readiness),
                        "gpu_during": cast(JsonValue, gpu_during),
                        "cleanup": cast(JsonValue, cleanup),
                        "gpu_after": cast(JsonValue, gpu_after),
                        "snapshot_pre": snapshot_pre_ref,
                        "snapshot_post": cast(JsonValue, snapshot_post_ref),
                        "runtime_scratch_pre": scratch_pre_refs[model_ordinal - 1],
                        "runtime_scratch_post": cast(JsonValue, scratch_post_ref),
                        "server_log": cast(JsonValue, log_ref),
                        "server_environment": cast(JsonValue, server_environment_ref),
                        "immediate_launch_preflight": cast(
                            JsonValue,
                            immediate_launch_preflight_ref,
                        ),
                        "foreign_process_target_count": cast(
                            JsonValue,
                            lifecycle_signal_summary["foreign_process_target_count"],
                        ),
                        "broad_signal_used": (
                            lifecycle_signal_summary["broad_process_signal_count"] != 0
                        ),
                    }
                    lifecycle_ref = _json_object_ref(store, lifecycle)
                    lifecycle_refs.append(lifecycle_ref)
                    store.event(
                        "SERVICE_LIFECYCLE_CLOSED",
                        {"model_id": model_id, "lifecycle": lifecycle_ref},
                    )
                if service_release_proven:
                    current_process = None
                    current_guard = None
                    current_cleanup_guard = None
            if cleanup_error is not None:
                _raise_captured(cleanup_error)
            if model_error is not None:
                _raise_captured(model_error)

        runtime_tree_post_ref = _json_object_ref(
            store,
            operations.verify_runtime_trees_post(
                authority,
                phase="PASS_POSTFLIGHT",
            ),
        )
        operations.assert_port_free()
        final_device = operations.inspect_gpu(authority)
        final_gpu_processes = operations.inspect_gpu_processes(
            authority, owned_pids=launch_tree_pids
        )
        final_isolation = operations.assert_gpu_isolation(
            baseline_gpu_processes,
            final_gpu_processes,
            owned_pids=launch_tree_pids,
            require_owned_absent=True,
        )
        launcher_scratch_post_ref = _json_object_ref(
            store,
            operations.inspect_launcher_scratch(
                authority,
                run_id,
                "PASS_POSTFLIGHT",
            ),
        )
        signal_summary = _signal_trace_summary(signal_trace)
        signal_targets = set(cast(list[int], signal_summary["signal_target_pids"]))
        signal_trace_ref = _json_object_ref(
            store,
            {
                "schema_version": "mobileworld.g1.gpu-live-smoke-signal-trace/v1",
                "run_id": run_id,
                "events": cast(JsonValue, signal_trace),
                **signal_summary,
            },
        )
        network_summary = _network_observation_summary(network_observations)
        network_observations_ref = _json_object_ref(
            store,
            {
                "schema_version": "mobileworld.g1.gpu-live-smoke-network-observations/v1",
                "run_id": run_id,
                "observations": cast(JsonValue, network_observations),
                **network_summary,
            },
        )
        socket_summary = _socket_observation_summary(socket_observations)
        socket_observations_ref = _json_object_ref(
            store,
            {
                "schema_version": "mobileworld.g1.gpu-live-smoke-socket-observations/v1",
                "run_id": run_id,
                "observations": cast(JsonValue, socket_observations),
                **socket_summary,
            },
        )
        if not (
            logical_calls == 22
            and physical_requests == 22
            and len(call_refs) == 22
            and len(lifecycle_refs) == 2
            and len(snapshot_pre_refs) == 2
            and len(snapshot_post_refs) == 2
            and len(scratch_pre_refs) == 2
            and len(scratch_post_refs) == 2
            and len(server_environment_refs) == 2
            and namespace_ref is not None
            and launcher_scratch_post_ref is not None
            and runtime_tree_post_ref is not None
            and (
                not bootstrap_receipts_required
                or (stage1_preexec_ref is not None and preimport_runtime_ref is not None)
            )
            and signal_targets.issubset(launch_tree_pids)
            and signal_summary["foreign_process_target_count"] == 0
            and signal_summary["broad_process_signal_count"] == 0
            and signal_summary["pidfd_only"] is True
            and network_summary["request_observation_count"] == 22
            and network_summary["exact_loopback_request_count"] == 22
            and network_summary["non_loopback_connection_count"] == 0
            and socket_summary["socket_observation_count"] == 46
            and socket_summary["non_loopback_inet_socket_count"] == 0
            and len(credential_scan_refs) == 24
            and server_log_capture_expected == 2
            and server_log_capture_succeeded == 2
        ):
            _fail(
                "GPU_SMOKE_PUBLICATION_CLOSURE_FAILED",
                "PASS evidence does not close the exact 22-call/two-lifecycle census",
            )
        final_device_ref = _json_object_ref(store, final_device)
        final_gpu_ref = _json_object_ref(store, final_gpu_processes)
        owned_command_receipts = store.owned_command_references()
        operation_ledger: dict[str, JsonValue] = {
            "model_launch_count": 2,
            "logical_call_count": logical_calls,
            "physical_http_request_count": physical_requests,
            "sdk_hidden_retry_count": 0,
            "owned_command_receipt_count": len(owned_command_receipts),
            "owned_command_receipts": owned_command_receipts,
            "signal_target_pids": sorted(signal_targets),
            "signal_sent_pids": cast(JsonValue, signal_summary["signal_sent_pids"]),
            "signal_intent_count": cast(JsonValue, signal_summary["signal_intent_count"]),
            "signal_sent_count": cast(JsonValue, signal_summary["signal_sent_count"]),
            "signal_trace": signal_trace_ref,
            "recorded_launch_tree_pids": sorted(launch_tree_pids),
            "signal_targets_subset_of_launch_trees": signal_targets.issubset(launch_tree_pids),
            "foreign_process_target_count": cast(
                JsonValue, signal_summary["foreign_process_target_count"]
            ),
            "broad_process_signal_count": cast(
                JsonValue, signal_summary["broad_process_signal_count"]
            ),
            "request_observation_count": cast(
                JsonValue, network_summary["request_observation_count"]
            ),
            "network_observations": network_observations_ref,
            "non_loopback_connection_count": cast(
                JsonValue, network_summary["non_loopback_connection_count"]
            ),
            "socket_observation_count": cast(JsonValue, socket_summary["socket_observation_count"]),
            "socket_observations": socket_observations_ref,
            "non_loopback_inet_socket_count": cast(
                JsonValue, socket_summary["non_loopback_inet_socket_count"]
            ),
            "credential_read_count": 0,
            "credential_scan_count": len(credential_scan_refs),
            "credential_scan_receipts": credential_scan_refs,
            "network_namespace": namespace_ref,
            "runtime_scratch_pre_receipts": scratch_pre_refs,
            "runtime_scratch_post_receipts": scratch_post_refs,
            "launcher_scratch_post": launcher_scratch_post_ref,
            "runtime_tree_post": runtime_tree_post_ref,
            "stage1_preexec": cast(JsonValue, stage1_preexec_ref),
            "stage2_preimport": cast(JsonValue, preimport_runtime_ref),
            "client_environment": namespace_ref,
            "server_environment_receipts": server_environment_refs,
            "runtime_scratch_preserved_for_audit": True,
            "inet_inet6_external_network_mechanically_unavailable": True,
            "server_log_capture_complete": (
                server_log_capture_succeeded == server_log_capture_expected == 2
            ),
            "generated_action_execution_count": 0,
            "mobileworld_action_count": 0,
            "replay_count": 0,
            "backend_restore_count": 0,
            "response_feedback_count": 0,
        }
        store.event(
            "RUN_PASS_VALIDATED",
            {
                "call_receipt_count": len(call_refs),
                "lifecycle_receipt_count": len(lifecycle_refs),
                "final_gpu_device": final_device_ref,
                "final_gpu_processes": final_gpu_ref,
                "final_gpu_isolation": final_isolation,
                "operation_ledger": operation_ledger,
            },
        )
        terminal = store.seal(
            "PASS",
            {
                "decision_id": DECISION_ID,
                "authority_id": authority.authority_id,
                "authority_sha256": authority.sha256,
                "smoke_packet_sha256": packet.sha256,
                "started_at_utc": started_at,
                "finished_at_utc": _utc_now(),
                "model_order": list(MODEL_ORDER),
                "logical_call_count": logical_calls,
                "physical_http_request_count": physical_requests,
                "sdk_hidden_retry_count": 0,
                "lifecycle_receipts": lifecycle_refs,
                "call_receipts": call_refs,
                "snapshot_pre_receipts": snapshot_pre_refs,
                "snapshot_post_receipts": snapshot_post_refs,
                "network_namespace": namespace_ref,
                "runtime_scratch_pre_receipts": scratch_pre_refs,
                "runtime_scratch_post_receipts": scratch_post_refs,
                "launcher_scratch_post": launcher_scratch_post_ref,
                "runtime_tree_post": runtime_tree_post_ref,
                "stage1_preexec": cast(JsonValue, stage1_preexec_ref),
                "stage2_preimport": cast(JsonValue, preimport_runtime_ref),
                "client_environment": namespace_ref,
                "server_environment_receipts": server_environment_refs,
                "gpu_preflight": baseline_gpu_processes_ref,
                "gpu_postflight": final_gpu_ref,
                "operation_ledger": operation_ledger,
                "formal_model_immutability_proven": False,
                "toctou_free_model_binding_proven": False,
                "formal_runtime_immutability_proven": False,
                "toctou_free_runtime_binding_proven": False,
                "native_dt_needed_dependency_closure_proven": False,
                "generated_action_executed": False,
                "replay_executed": False,
                "evidence_closure_proven": True,
                "own_service_release_proven": True,
            },
            validation_documents={
                "authority": authority.value,
                "packet": packet.value,
                "preparation": preparation,
            },
        )
        return terminal
    except BaseException as caught:
        if isinstance(caught, GpuLiveSmokeError) and id(caught) not in (
            signal_trace_consumed_error_ids
        ):
            for item in _owned_command_signal_trace_from_value(caught.execution_detail):
                signal_trace.append(
                    {
                        **item,
                        "global_sequence": len(signal_trace) + 1,
                        "cleanup_attempt": "owned-auxiliary-command-failure",
                    }
                )
        if current_failed_launch is not None:
            try:
                current_failed_launch, acquisition_receipt = (
                    operations.freeze_failed_launch_acquisition(
                        authority,
                        current_failed_launch,
                    )
                )
                acquisition_ref = _json_object_ref(store, acquisition_receipt)
                failed_launch_pids = {item.pid for item in current_failed_launch.recorded_tree}
                launch_tree_pids.update(failed_launch_pids)
                store.event(
                    "PROVISIONAL_ACQUISITION_FROZEN",
                    {
                        "model_id": current_failed_launch.model_id,
                        "acquisition": acquisition_ref,
                        "cleanup_eligible_pids": sorted(failed_launch_pids),
                        "service_launched": False,
                        "emergency_retry": True,
                    },
                )
                failed_launch_cleanup = operations.cleanup_failed_launch(
                    authority,
                    current_failed_launch,
                )
                _extend_signal_trace(
                    signal_trace,
                    failed_launch_cleanup,
                    cleanup_attempt="failed-launch-emergency",
                )
                failed_launch_signaled = {
                    cast(int, pid)
                    for key in ("term_pids", "kill_pids")
                    for pid in cast(list[JsonValue], failed_launch_cleanup.get(key, []))
                }
                launch_tree_pids.update(failed_launch_pids | failed_launch_signaled)
                if not failed_launch_signaled.issubset(failed_launch_pids):
                    _fail(
                        "GPU_SMOKE_FOREIGN_PROCESS_TARGET_ATTEMPT",
                        "failed-launch emergency target is outside its recorded tree",
                    )
                signal_targets.update(failed_launch_signaled)
                operations.close_failed_launch_acquisition(current_failed_launch)
                store.event(
                    "PROVISIONAL_ACQUISITION_CLEANUP",
                    {
                        "model_id": current_failed_launch.model_id,
                        "acquisition": acquisition_ref,
                        "cleanup": failed_launch_cleanup,
                        "service_launched": False,
                        "release_proven": True,
                        "emergency_retry": True,
                    },
                )
                if runtime_scratch is not None:
                    released_model_id = current_failed_launch.model_id
                    scratch_post_refs.append(
                        _json_object_ref(
                            store,
                            operations.inspect_runtime_scratch(
                                authority,
                                run_id,
                                released_model_id,
                            ),
                        )
                    )
                    scratch_post_model_ids.add(released_model_id)
                current_failed_launch = None
            except BaseException as emergency_error:
                if isinstance(emergency_error, GpuLiveSmokeError):
                    _extend_signal_trace(
                        signal_trace,
                        emergency_error.execution_detail,
                        cleanup_attempt="failed-launch-emergency-failed",
                    )
                store.event(
                    "EMERGENCY_CLEANUP_FAILED",
                    {
                        "error_code": (
                            emergency_error.code
                            if isinstance(emergency_error, GpuLiveSmokeError)
                            else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                        ),
                        "failed_launch": True,
                        "no_unproven_process_signaled": True,
                    },
                )
        if (
            current_process is not None
            and current_guard is not None
            and current_cleanup_guard is None
        ):
            store.event(
                "EMERGENCY_CLEANUP_FAILED",
                {
                    "error_code": "GPU_SMOKE_PROCESS_TREE_UNBOUND",
                    "no_unproven_process_signaled": True,
                    "persisted_cleanup_eligibility": False,
                    "signal_attempt_count": 0,
                },
            )
        elif (
            current_process is not None
            and current_guard is not None
            and current_cleanup_guard is not None
        ):
            try:
                owned_pids = {item.pid for item in current_cleanup_guard.service_tree}
                launch_tree_pids.update(owned_pids)
                emergency_cleanup = operations.stop_service(
                    current_cleanup_guard,
                    current_process,
                )
                _extend_signal_trace(
                    signal_trace,
                    emergency_cleanup,
                    cleanup_attempt="emergency",
                )
                emergency_signaled = {
                    cast(int, pid)
                    for key in ("term_pids", "kill_pids")
                    for pid in cast(list[JsonValue], emergency_cleanup.get(key, []))
                }
                if not emergency_signaled.issubset(owned_pids):
                    _fail(
                        "GPU_SMOKE_FOREIGN_PROCESS_TARGET_ATTEMPT",
                        "emergency cleanup target is outside the recorded service tree",
                    )
                signal_targets.update(emergency_signaled)
                if baseline_gpu_processes is None:
                    _fail(
                        "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED",
                        "GPU baseline is absent during emergency service cleanup",
                    )
                emergency_gpu_after = operations.inspect_gpu_processes(
                    authority, owned_pids=owned_pids
                )
                emergency_gpu_release = operations.assert_gpu_isolation(
                    baseline_gpu_processes,
                    emergency_gpu_after,
                    owned_pids=owned_pids,
                    require_owned_absent=True,
                )
                operations.assert_port_free()
                emergency_cleanup["gpu_release"] = emergency_gpu_release
                store.event(
                    "EMERGENCY_OWN_SERVICE_CLEANUP",
                    {
                        "model_id": current_guard.model_id,
                        "guard": current_cleanup_guard.to_dict(),
                        "cleanup": emergency_cleanup,
                        "persisted_cleanup_eligibility": True,
                    },
                )
                current_process = None
                current_guard = None
                current_cleanup_guard = None
            except BaseException as emergency_error:
                if isinstance(emergency_error, GpuLiveSmokeError):
                    _extend_signal_trace(
                        signal_trace,
                        emergency_error.execution_detail,
                        cleanup_attempt="emergency-failed",
                    )
                store.event(
                    "EMERGENCY_CLEANUP_FAILED",
                    {
                        "error_code": (
                            emergency_error.code
                            if isinstance(emergency_error, GpuLiveSmokeError)
                            else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                        ),
                        "no_unproven_process_signaled": True,
                    },
                )
        released_all_owned_services = (
            current_failed_launch is None
            and current_process is None
            and current_guard is None
            and current_cleanup_guard is None
        )
        if released_all_owned_services:
            # A primary cleanup may fail after releasing part of the exact
            # tree, and its emergency retry may finish outside the model
            # finally block.  Close every already-opened immutable/scratch
            # census only after GPU/process/port release has been proven.
            for released_model_id, snapshot_pre in snapshot_pre_by_model.items():
                if released_model_id in snapshot_post_model_ids:
                    continue
                try:
                    recovered_snapshot_post = operations.verify_snapshot(
                        authority,
                        receipt,
                        released_model_id,
                    )
                    if canonical_json_bytes(snapshot_pre) != canonical_json_bytes(
                        recovered_snapshot_post
                    ):
                        _fail(
                            "GPU_SMOKE_SNAPSHOT_PRE_POST_MISMATCH",
                            "model snapshot changed before failure evidence closure",
                        )
                    snapshot_post_refs.append(_json_object_ref(store, recovered_snapshot_post))
                    snapshot_post_model_ids.add(released_model_id)
                except BaseException as snapshot_error:
                    store.event(
                        "RUNTIME_TREE_CENSUS_FAILED",
                        {
                            "artifact_kind": "MODEL_SNAPSHOT_POST",
                            "model_id": released_model_id,
                            "error_code": (
                                snapshot_error.code
                                if isinstance(snapshot_error, GpuLiveSmokeError)
                                else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                            ),
                        },
                    )
            if runtime_scratch is not None:
                for released_model_id in started_model_ids:
                    if released_model_id in scratch_post_model_ids:
                        continue
                    try:
                        recovered_scratch_post = operations.inspect_runtime_scratch(
                            authority,
                            run_id,
                            released_model_id,
                        )
                        scratch_post_refs.append(_json_object_ref(store, recovered_scratch_post))
                        scratch_post_model_ids.add(released_model_id)
                    except BaseException as scratch_error:
                        store.event(
                            "RUNTIME_SCRATCH_CENSUS_FAILED",
                            {
                                "artifact_kind": "MODEL_RUNTIME_SCRATCH_POST",
                                "model_id": released_model_id,
                                "error_code": (
                                    scratch_error.code
                                    if isinstance(scratch_error, GpuLiveSmokeError)
                                    else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                                ),
                            },
                        )
        if (
            current_log is not None
            and current_failed_launch is None
            and current_process is None
            and current_guard is None
        ):
            try:
                log_bytes = _read_log_bytes(current_log)
                credential_scan_refs.append(
                    _json_object_ref(
                        store,
                        _scan_bytes_for_credentials(
                            log_bytes,
                            artifact_kind="SERVER_LOG",
                        ),
                    )
                )
                store.object(log_bytes, "text/plain; charset=utf-8")
                server_log_capture_succeeded += 1
            except BaseException as log_error:
                store.event(
                    "SERVER_LOG_CAPTURE_FAILED",
                    {
                        "error_code": (
                            log_error.code
                            if isinstance(log_error, GpuLiveSmokeError)
                            else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                        )
                    },
                )
            finally:
                current_log.close()
                current_log = None
        try:
            launcher_scratch_post_ref = _json_object_ref(
                store,
                operations.inspect_launcher_scratch(
                    authority,
                    run_id,
                    "FAIL_POSTFLIGHT",
                ),
            )
        except BaseException as scratch_error:
            store.event(
                "RUNTIME_SCRATCH_CENSUS_FAILED",
                {
                    "error_code": (
                        scratch_error.code
                        if isinstance(scratch_error, GpuLiveSmokeError)
                        else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                    )
                },
            )
        try:
            runtime_tree_post_ref = _json_object_ref(
                store,
                operations.verify_runtime_trees_post(
                    authority,
                    phase="FAIL_POSTFLIGHT",
                ),
            )
        except BaseException as runtime_tree_error:
            store.event(
                "RUNTIME_TREE_CENSUS_FAILED",
                {
                    "error_code": (
                        runtime_tree_error.code
                        if isinstance(runtime_tree_error, GpuLiveSmokeError)
                        else "GPU_SMOKE_UNCLASSIFIED_FAILURE"
                    )
                },
            )
        live_writer_log_capture_skipped = current_log is not None
        if live_writer_log_capture_skipped:
            store.event(
                "SERVER_LOG_CAPTURE_SKIPPED_UNCLOSED_WRITER",
                {
                    "failed_launch_handle_retained": current_failed_launch is not None,
                    "service_guard_retained": (
                        current_process is not None and current_guard is not None
                    ),
                    "persisted_cleanup_guard_retained": current_cleanup_guard is not None,
                    "log_completeness_claimed": False,
                },
            )
        server_log_capture_complete = (
            not live_writer_log_capture_skipped
            and server_log_capture_succeeded == server_log_capture_expected
        )
        own_service_release_proven = (
            current_failed_launch is None
            and current_process is None
            and current_guard is None
            and current_cleanup_guard is None
        )
        attempt_receipt_closure_proven = physical_requests <= logical_calls <= len(call_refs)
        snapshot_closure_proven = len(snapshot_post_refs) == len(snapshot_pre_refs)
        scratch_closure_proven = len(scratch_post_refs) == server_log_capture_expected
        failure_artifact_closure_proven = (
            attempt_receipt_closure_proven
            and snapshot_closure_proven
            and scratch_closure_proven
            and server_log_capture_complete
            and own_service_release_proven
            and namespace_ref is not None
            and launcher_scratch_post_ref is not None
            and runtime_tree_post_ref is not None
        )
        error = (
            caught
            if isinstance(caught, GpuLiveSmokeError)
            else GpuLiveSmokeError(
                "GPU_SMOKE_UNCLASSIFIED_FAILURE",
                f"unclassified live-smoke failure ({type(caught).__name__})",
            )
        )
        signal_summary = _signal_trace_summary(signal_trace)
        signal_targets = set(cast(list[int], signal_summary["signal_target_pids"]))
        signal_trace_ref = _json_object_ref(
            store,
            {
                "schema_version": "mobileworld.g1.gpu-live-smoke-signal-trace/v1",
                "run_id": run_id,
                "events": cast(JsonValue, signal_trace),
                **signal_summary,
            },
        )
        network_summary = _network_observation_summary(network_observations)
        network_observations_ref = _json_object_ref(
            store,
            {
                "schema_version": "mobileworld.g1.gpu-live-smoke-network-observations/v1",
                "run_id": run_id,
                "observations": cast(JsonValue, network_observations),
                **network_summary,
            },
        )
        socket_summary = _socket_observation_summary(socket_observations)
        socket_observations_ref = _json_object_ref(
            store,
            {
                "schema_version": "mobileworld.g1.gpu-live-smoke-socket-observations/v1",
                "run_id": run_id,
                "observations": cast(JsonValue, socket_observations),
                **socket_summary,
            },
        )
        store.event(
            "RUN_FAILED",
            {
                "error_code": error.code,
                "json_path": error.json_path,
                "error_detail": error.execution_detail,
                "logical_call_count": logical_calls,
                "physical_http_request_count": physical_requests,
                "foreign_process_target_count": cast(
                    JsonValue, signal_summary["foreign_process_target_count"]
                ),
                "non_loopback_connection_count": cast(
                    JsonValue, network_summary["non_loopback_connection_count"]
                ),
                "generated_action_executed": False,
            },
        )
        owned_command_receipts = store.owned_command_references()
        terminal = store.seal(
            "FAIL",
            {
                "decision_id": DECISION_ID,
                "authority_id": authority.authority_id,
                "authority_sha256": authority.sha256,
                "smoke_packet_sha256": packet.sha256,
                "started_at_utc": started_at,
                "finished_at_utc": _utc_now(),
                "model_order": list(MODEL_ORDER),
                "logical_call_count": logical_calls,
                "physical_http_request_count": physical_requests,
                "sdk_hidden_retry_count": 0,
                "lifecycle_receipts": lifecycle_refs,
                "call_receipts": call_refs,
                "snapshot_pre_receipts": snapshot_pre_refs,
                "snapshot_post_receipts": snapshot_post_refs,
                "network_namespace": cast(JsonValue, namespace_ref),
                "runtime_scratch_pre_receipts": scratch_pre_refs,
                "runtime_scratch_post_receipts": scratch_post_refs,
                "launcher_scratch_post": cast(JsonValue, launcher_scratch_post_ref),
                "runtime_tree_post": cast(JsonValue, runtime_tree_post_ref),
                "stage1_preexec": cast(JsonValue, stage1_preexec_ref),
                "stage2_preimport": cast(JsonValue, preimport_runtime_ref),
                "client_environment": cast(JsonValue, namespace_ref),
                "server_environment_receipts": server_environment_refs,
                "error_code": error.code,
                "error_json_path": error.json_path,
                "error_detail": error.execution_detail,
                "operation_ledger": {
                    "owned_command_receipt_count": len(owned_command_receipts),
                    "owned_command_receipts": owned_command_receipts,
                    "signal_target_pids": sorted(signal_targets),
                    "signal_sent_pids": cast(JsonValue, signal_summary["signal_sent_pids"]),
                    "signal_intent_count": cast(JsonValue, signal_summary["signal_intent_count"]),
                    "signal_sent_count": cast(JsonValue, signal_summary["signal_sent_count"]),
                    "signal_trace": signal_trace_ref,
                    "recorded_launch_tree_pids": sorted(launch_tree_pids),
                    "foreign_process_target_count": cast(
                        JsonValue, signal_summary["foreign_process_target_count"]
                    ),
                    "broad_process_signal_count": cast(
                        JsonValue, signal_summary["broad_process_signal_count"]
                    ),
                    "request_observation_count": cast(
                        JsonValue, network_summary["request_observation_count"]
                    ),
                    "network_observations": network_observations_ref,
                    "non_loopback_connection_count": cast(
                        JsonValue, network_summary["non_loopback_connection_count"]
                    ),
                    "socket_observation_count": cast(
                        JsonValue, socket_summary["socket_observation_count"]
                    ),
                    "socket_observations": socket_observations_ref,
                    "non_loopback_inet_socket_count": cast(
                        JsonValue, socket_summary["non_loopback_inet_socket_count"]
                    ),
                    "credential_read_count": 0,
                    "credential_scan_count": len(credential_scan_refs),
                    "credential_scan_receipts": credential_scan_refs,
                    "network_namespace": cast(JsonValue, namespace_ref),
                    "runtime_scratch_pre_receipts": scratch_pre_refs,
                    "runtime_scratch_post_receipts": scratch_post_refs,
                    "launcher_scratch_post": cast(JsonValue, launcher_scratch_post_ref),
                    "runtime_tree_post": cast(JsonValue, runtime_tree_post_ref),
                    "stage1_preexec": cast(JsonValue, stage1_preexec_ref),
                    "stage2_preimport": cast(JsonValue, preimport_runtime_ref),
                    "client_environment": cast(JsonValue, namespace_ref),
                    "server_environment_receipts": server_environment_refs,
                    "runtime_scratch_preserved_for_audit": True,
                    "inet_inet6_external_network_mechanically_unavailable": (
                        namespace_ref is not None
                    ),
                    "server_log_capture_complete": server_log_capture_complete,
                    "attempt_receipt_closure_proven": attempt_receipt_closure_proven,
                    "snapshot_closure_proven": snapshot_closure_proven,
                    "scratch_closure_proven": scratch_closure_proven,
                    "generated_action_execution_count": 0,
                    "mobileworld_action_count": 0,
                    "replay_count": 0,
                    "backend_restore_count": 0,
                    "response_feedback_count": 0,
                },
                "formal_model_immutability_proven": False,
                "toctou_free_model_binding_proven": False,
                "formal_runtime_immutability_proven": False,
                "toctou_free_runtime_binding_proven": False,
                "native_dt_needed_dependency_closure_proven": False,
                "generated_action_executed": False,
                "replay_executed": False,
                "evidence_closure_proven": failure_artifact_closure_proven,
                "own_service_release_proven": own_service_release_proven,
            },
            validation_documents={
                "authority": authority.value,
                "packet": packet.value,
                "preparation": preparation,
            },
        )
        error.terminal_receipt = terminal
        if current_log is not None:
            current_log.close()
            current_log = None
        if current_failed_launch is not None and current_failed_launch.acquisition_pidfd >= 0:
            try:
                operations.close_failed_launch_acquisition(current_failed_launch)
            except GpuLiveSmokeError:
                pass
        if error is caught:
            raise error
        raise error from caught


def execute_gpu_live_smoke(
    *,
    authority_path: str | os.PathLike[str],
    authority_sha256: str,
    smoke_packet_path: str | os.PathLike[str],
    model_config_manifest_path: str | os.PathLike[str],
    operations: GpuLiveSmokeOperations | None = None,
    stage1_preexec_receipt: dict[str, JsonValue] | None = None,
    preimport_runtime_receipt: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Execute only the exact owner-authorized synthetic 22-call live proof."""

    real_execution = operations is None
    if (
        real_execution
        and _absolute_lexical(os.fspath(authority_path), "$.authority_path")
        != LAUNCH_SHIM_AUTHORITY_PATH
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "real execution authority path bypasses the frozen static shim token",
        )
    authority = load_gpu_live_authority(authority_path, authority_sha256)
    if real_execution and not (
        _absolute_lexical(os.fspath(smoke_packet_path), "$.smoke_packet_path")
        == authority.launch_shim["smoke_packet_path"]
        and _absolute_lexical(
            os.fspath(model_config_manifest_path),
            "$.model_config_manifest_path",
        )
        == authority.launch_shim["model_config_manifest_path"]
    ):
        _fail(
            "GPU_SMOKE_SOURCE_BINDING_INVALID",
            "real execution inputs differ from the static launch shim binding",
        )
    packet = load_gpu_smoke_packet(
        smoke_packet_path,
        cast(str, authority.bindings["smoke_packet_sha256"]),
        g1_5_seed=cast(int, authority.matrix["g1_5_seed"]),
    )
    return _execute_authorized_gpu_live_smoke(
        authority,
        packet,
        model_config_manifest_path,
        operations=GpuLiveSmokeOperations() if operations is None else operations,
        stage1_preexec_receipt=stage1_preexec_receipt,
        preimport_runtime_receipt=preimport_runtime_receipt,
        bootstrap_receipts_required=real_execution,
    )


__all__ = [
    "AUTHORITY_SCHEMA_VERSION",
    "AUTHORIZED_GPU_UUID",
    "AUTHORIZED_SCOPE",
    "DECISION_ID",
    "EXECUTION_RECEIPT_SCHEMA_VERSION",
    "G1_5_ARMS",
    "GpuLiveAuthority",
    "GpuLiveSmokeError",
    "GpuLiveSmokeOperations",
    "GpuSmokeCall",
    "GpuSmokePacket",
    "LAUNCH_SHIM_AUTHORITY_PATH",
    "LAUNCH_SHIM_PATH",
    "LAUNCH_SHIM_SCHEMA_VERSION",
    "LAUNCH_SHIM_TOKEN_PREFIX",
    "MODEL_ORDER",
    "MINIMUM_FREE_MEMORY_BYTES",
    "PREPARATION_SCHEMA_VERSION",
    "REPLAY_SEEDS",
    "SMOKE_PACKET_SCHEMA_VERSION",
    "TOOL_SHELL_PATH",
    "TOOL_SHELL_RESOLVED_PATH",
    "TOOL_SHELL_SCHEMA_VERSION",
    "build_gpu_live_smoke_launch_shim",
    "build_private_runtime",
    "compile_gpu_smoke_packet",
    "execute_gpu_live_smoke",
    "inspect_authority_inputs",
    "load_gpu_live_authority",
    "load_gpu_smoke_packet",
    "prepare_gpu_live_smoke",
    "render_gpu_live_smoke_tool_command",
    "write_gpu_smoke_packet",
]
