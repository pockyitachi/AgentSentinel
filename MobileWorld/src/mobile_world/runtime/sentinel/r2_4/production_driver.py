"""Sealed production driver for the R2.4 smoke and R2.5 pilot.

The module owns fixed-argv model/backend lifecycle control, per-dispatch
identity checks, live host execution, and deterministic CPU test doubles. Public
factories accept no arbitrary command, callback, URL, client, or secret value;
actual I/O remains reachable only through an owner-confirmed authority chain.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.metadata
import io
import json
import math
import os
import pwd
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlsplit

from openai import DefaultHttpxClient, OpenAI
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations.mai_ui_agent import (
    MAIUINaivigationAgent,
)
from mobile_world.agents.implementations.mai_ui_agent import (
    parse_action_to_structure_output as parse_mai_action,
)
from mobile_world.agents.implementations.qwen3vl import (
    Qwen3VLAgentMCP,
    parsing_response_to_andoid_world_env_action,
)
from mobile_world.agents.implementations.qwen3vl import (
    parse_action_to_structure_output as parse_qwen_action,
)
from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.audit.config import AuditConfig
from mobile_world.runtime.audit.context import (
    AuditContext,
    ModelCallTrace,
    bind_audit_context,
)
from mobile_world.runtime.audit.lifecycle import (
    AuditLifecycle,
    TaskAuditBinding,
    bootstrap_audit_run,
)
from mobile_world.runtime.client import (
    AndroidEnvClient,
    CleanupTaskTeardownResultV1,
    CleanupTaskTeardownStatusV1,
)
from mobile_world.runtime.sentinel import (
    NoOpSentinelPolicy,
    PromptSentinel,
    SentinelCallRole,
    SentinelFallbackReason,
    SentinelHostConfig,
    SentinelLogicalCall,
    SentinelMode,
    bind_sentinel_logical_call,
)
from mobile_world.runtime.sentinel.r2_4.capabilities import build_runtime_history_codec_resolver
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes, canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1,
    LiveAttemptPricingV1,
    LiveAttemptRoleV1,
    live_attempt_pricing_sha256,
    live_attempt_receipt_projection,
    live_attempt_receipt_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    AdapterStageResultV1,
    CaseAuthorityBrokerV1,
    CaseExecutionLeaseBindingV1,
    LiveSmokeAdapterPortV1,
    PilotAdapterPortV1,
    StageAdapterContextV1,
)
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    OwnerAuthorizedLivePerCallPolicyV1,
    ProductionLiveBudgetLedgerV1,
    build_owner_authorized_live_per_call_policy_v1,
    build_production_live_budget_ledger_v1,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    HostLiveSmokePlanV1,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    RunStageV1,
    SecretFileReferenceV1,
    SmokeModeV1,
    SnapshotResourceV1,
    compute_snapshot_tree_digest,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    ExternalProductionRuntimeAuditSinkV1,
    ProductionRuntimeAuditAdmissionFailureReceiptV1,
    ProductionRuntimeAuditCommitFailureReceiptV1,
    ProductionRuntimeAuditFailureReceiptV1,
    ProductionRuntimeAuditPreProviderOutcomeV1,
    ProductionRuntimeAuditPreProviderStatusV1,
    ProductionRuntimeAuditReceiptV1,
    ProductionRuntimeAuditV1,
    production_runtime_audit_admission_failure_receipt_projection,
    production_runtime_audit_admission_failure_receipt_sha256,
    production_runtime_audit_commit_failure_receipt_projection,
    production_runtime_audit_commit_failure_receipt_sha256,
    production_runtime_audit_failure_receipt_projection,
    production_runtime_audit_failure_receipt_sha256,
    production_runtime_audit_receipt_projection,
    production_runtime_audit_receipt_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    CaseExecutionLeaseV1,
    ProductionPostPreflightFactoryV1,
    case_execution_lease_sha256,
)
from mobile_world.runtime.sentinel.r2_4.run_fatal import (
    ProductionRunFatalError,
    build_production_run_fatal_latch_v1,
    production_run_fatal_state_projection,
    production_run_fatal_state_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FrozenPilotManifestV1,
    PilotArmV1,
    PilotCellV1,
    PilotHostV1,
    PilotResetTaskInitInputV1,
    ResolvedPilotTaskInputsV1,
    frozen_pilot_manifest_projection,
    frozen_pilot_manifest_sha256,
    parse_frozen_pilot_manifest,
    resolve_pilot_task_inputs_v1,
    resolved_pilot_task_inputs_sha256,
)
from mobile_world.runtime.sentinel.sidecar import ExternalSentinelReceiptSink
from mobile_world.runtime.utils.models import (
    ANSWER,
    CLICK,
    DOUBLE_TAP,
    DRAG,
    ENV_FAIL,
    FINISHED,
    INPUT_TEXT,
    KEYBOARD_ENTER,
    LONG_PRESS,
    MCP,
    NAVIGATE_BACK,
    NAVIGATE_HOME,
    OPEN_APP,
    SCROLL,
    STATUS,
    SWIPE,
    UNKNOWN,
    WAIT,
    JSONAction,
    Observation,
)

PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-r2.5-production-driver-evidence/v1"
)
PRODUCTION_SHARED_RESOURCE_EVIDENCE_SCHEMA_VERSION_V2: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-shared-resource-evidence/v2"
)
PRODUCTION_SHARED_SMOKE_EVIDENCE_SCHEMA_VERSION_V2: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-shared-smoke-evidence/v2"
)
PRODUCTION_MODEL_HANDOFF_EVIDENCE_SCHEMA_VERSION_V1: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-model-handoff-evidence/v1"
)
PRODUCTION_RESOURCE_CLEANUP_EVIDENCE_SCHEMA_VERSION_V1: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-resource-cleanup-evidence/v1"
)
PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-resource-cleanup-bound/v1"
)
OFFICIAL_RESULT_EVALUATOR_ID_V1: Final[str] = "mobileworld.task.official-success/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_MODULE_SEAL: Final[object] = object()
_PRODUCTION_INSTALLATION_SEAL: Final[object] = object()
_VLLM_MAX_MODEL_LEN: Final[int] = 32_768
_VLLM_MAX_BATCHED_TOKENS: Final[int] = 8_192
_SHARED_VLLM_GPU_MEMORY_UTILIZATION_V1: Final[str] = "0.24"
_SHARED_MINIMUM_FREE_GPU_MEMORY_MIB_V1: Final[int] = 51_200
_NVIDIA_MEMORY_ACCOUNTING_TOLERANCE_MIB_V1: Final[int] = 4
_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1: Final[int] = 15
_RESOURCE_ATTESTATION_COMMAND_TIMEOUT_SECONDS_V1: Final[int] = 30
_MOBILEWORLD_BACKEND_IMAGE: Final[str] = "mobile_world:reset"
_PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1: Final[int] = (
    PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1
)
_MOBILEWORLD_BACKEND_CONTAINER_PORT: Final[int] = 6_800
_DOCKER_EXECUTABLE: Final[str] = "/usr/bin/docker"
_NVIDIA_SMI_EXECUTABLE: Final[str] = "/usr/bin/nvidia-smi"
_NVIDIA_GPU_UUID: Final[re.Pattern[str]] = re.compile(
    r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_DOCKER_NETWORK: Final[str] = "mwnet"
_MAX_HEALTH_RESPONSE_BYTES: Final[int] = 1_048_576
_MAX_INLINE_UNIT_EVIDENCE_JOURNAL_BYTES: Final[int] = 4 * 1024 * 1024
_UNIT_EVIDENCE_BLOB_REFERENCE_SCHEMA_VERSION: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-production-unit-evidence-blob-reference/v1"
)
_VALIDATED_UNIT_EVIDENCE_BLOB_REFERENCE_SCHEMA_VERSION: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-validated-unit-evidence-blob-reference/v1"
)
_UNIT_EVIDENCE_BLOB_STORAGE_KIND: Final[str] = "OWNER_ONLY_CONTENT_ADDRESSED_BLOB"
_UNIT_EVIDENCE_BLOB_SUFFIX: Final[str] = ".production-unit-evidence-blob.v1.json"
_PILOT_GUI_ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        ANSWER,
        CLICK,
        DOUBLE_TAP,
        DRAG,
        INPUT_TEXT,
        KEYBOARD_ENTER,
        LONG_PRESS,
        NAVIGATE_BACK,
        NAVIGATE_HOME,
        OPEN_APP,
        SCROLL,
        STATUS,
        SWIPE,
        WAIT,
    }
)


class ProductionDriverError(ValueError):
    """Stable, secret-free failure from the sealed driver boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ProductionDriverHookV1(StrEnum):
    """Closed implementation hooks that still need a reviewed resource port."""

    HASH_BOUND_SMOKE_FIXTURE_LOADER = "HASH_BOUND_SMOKE_FIXTURE_LOADER"
    POST_PREFLIGHT_CASE_LEASE_ISSUER = "POST_PREFLIGHT_CASE_LEASE_ISSUER"
    SEALED_HISTORY_POLICY_FACTORY = "SEALED_HISTORY_POLICY_FACTORY"
    CANCELLABLE_LIVE_ATTEMPT_RUNNER = "CANCELLABLE_LIVE_ATTEMPT_RUNNER"
    FROZEN_TASK_REGISTRY_RESOLVER = "FROZEN_TASK_REGISTRY_RESOLVER"
    ISOLATED_ANDROID_ENVIRONMENT_LEASE = "ISOLATED_ANDROID_ENVIRONMENT_LEASE"
    EXACT_QWEN_MAI_AGENT_FACTORY = "EXACT_QWEN_MAI_AGENT_FACTORY"
    MODE_BOUND_PROMPT_SENTINEL_FACTORY = "MODE_BOUND_PROMPT_SENTINEL_FACTORY"
    ACTOR_PROVIDER_AND_RUNTIME_AUDIT = "ACTOR_PROVIDER_AND_RUNTIME_AUDIT"
    PILOT_ONLY_ACTION_EXECUTOR = "PILOT_ONLY_ACTION_EXECUTOR"
    OFFICIAL_TASK_RESULT_READER = "OFFICIAL_TASK_RESULT_READER"
    HARD_BUDGET_CENSUS = "HARD_BUDGET_CENSUS"
    PER_UNIT_TEARDOWN_AND_RESET = "PER_UNIT_TEARDOWN_AND_RESET"


PRODUCTION_DRIVER_REQUIRED_HOOKS_V1: Final[tuple[ProductionDriverHookV1, ...]] = tuple(
    ProductionDriverHookV1
)

# These are descriptive bindings, never dynamically imported entry points.
PRODUCTION_DRIVER_REQUIRED_BINDINGS_V1: Final[tuple[str, ...]] = (
    "ProductionPostPreflightFactoryV1.issue_case_execution_lease -> exact CaseExecutionLeaseV1",
    "ProductionPostPreflightFactoryV1 child-only secret acquisition -> no parent-process key",
    "module-owned sealed R22OwnerAuthorizedLivePolicyAdapter factory -> manifest/case lease",
    "module-owned cancellable attempt runner -> exact terminal LiveAttemptReceiptV1",
    "runtime audit detail -> raw/final/provider/parser/action hash chain",
)


class CpuProductionDriverFaultV1(StrEnum):
    """Closed CPU-only failures; values cannot carry executable behavior."""

    NONE = "NONE"
    SMOKE_SHADOW_DISPATCH_FAILURE = "SMOKE_SHADOW_DISPATCH_FAILURE"
    SMOKE_SHADOW_CLEANUP_FAILURE = "SMOKE_SHADOW_CLEANUP_FAILURE"
    SMOKE_SHADOW_POST_DISPATCH_ADMISSION_FAILURE = "SMOKE_SHADOW_POST_DISPATCH_ADMISSION_FAILURE"
    PILOT_CELL_007_RESET_FAILURE = "PILOT_CELL_007_RESET_FAILURE"
    PILOT_CELL_001_RESET_STATE_DRIFT = "PILOT_CELL_001_RESET_STATE_DRIFT"
    PILOT_CELL_007_DISPATCH_FAILURE = "PILOT_CELL_007_DISPATCH_FAILURE"
    PILOT_CELL_007_CLEANUP_FAILURE = "PILOT_CELL_007_CLEANUP_FAILURE"
    PILOT_CELL_007_POST_DISPATCH_ADMISSION_FAILURE = (
        "PILOT_CELL_007_POST_DISPATCH_ADMISSION_FAILURE"
    )


class CpuResourceLifecycleFaultV1(StrEnum):
    """Closed resource faults for CPU-only ownership and recovery tests."""

    NONE = "NONE"
    BACKEND_START_TIMEOUT = "BACKEND_START_TIMEOUT"
    BACKEND_OWNERSHIP_MISMATCH = "BACKEND_OWNERSHIP_MISMATCH"
    MODEL_PARTIAL_START = "MODEL_PARTIAL_START"
    MODEL_PARTIAL_CLEANUP_ONCE = "MODEL_PARTIAL_CLEANUP_ONCE"
    MODEL_STOP_ONCE = "MODEL_STOP_ONCE"
    SHARED_GPU_CAPACITY_BELOW_MINIMUM = "SHARED_GPU_CAPACITY_BELOW_MINIMUM"
    HANDOFF_GPU_CAPACITY_BELOW_MINIMUM = "HANDOFF_GPU_CAPACITY_BELOW_MINIMUM"
    HANDOFF_MODEL_REAP_UNCONFIRMED = "HANDOFF_MODEL_REAP_UNCONFIRMED"
    SHARED_GPU_TENANT_DRIFT = "SHARED_GPU_TENANT_DRIFT"
    SHARED_GPU_TENANT_DRIFT_PERSISTS = "SHARED_GPU_TENANT_DRIFT_PERSISTS"
    MAI_PARTIAL_START = "MAI_PARTIAL_START"
    DISPATCH_MODEL_IDENTITY = "DISPATCH_MODEL_IDENTITY"
    DISPATCH_BACKEND_IDENTITY = "DISPATCH_BACKEND_IDENTITY"


class ProductionDispatchKindV1(StrEnum):
    """Closed physical resource boundaries that require a fresh liveness check."""

    ACTOR = "ACTOR"
    BACKEND_RESET = "BACKEND_RESET"
    BACKEND_TASK_GOAL = "BACKEND_TASK_GOAL"
    ACTION = "ACTION"
    SCORE = "SCORE"
    CLEANUP = "CLEANUP"


class ProductionResourceTopologyV1(StrEnum):
    """Closed GPU lifecycle topologies bound into the runtime configuration."""

    INDEPENDENT_GPU_CONCURRENT = "INDEPENDENT_GPU_CONCURRENT"
    SINGLE_GPU_SEQUENTIAL_SHARED = "SINGLE_GPU_SEQUENTIAL_SHARED"


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ProductionDriverError("INVALID_EVIDENCE", f"{name} is not lowercase SHA-256")
    return value


def _require_safe_id(value: object, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ProductionDriverError("INVALID_EVIDENCE", f"{name} is not a bounded safe ID")
    return value


def _require_nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ProductionDriverError("INVALID_CENSUS", f"{name} must be nonnegative")
    return value


def _hash_projection(domain: str, value: JsonValue) -> str:
    return canonical_sha256(
        cast(
            JsonValue,
            {
                "domain": domain,
                "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                "value": value,
            },
        )
    )


@dataclass(frozen=True, slots=True)
class _LoadedSmokeFixtureV1:
    request: dict[str, JsonValue]
    request_sha256: str
    byte_count: int
    task_instruction: str
    current_image_png: bytes


def _is_within_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_hash_bound_smoke_fixture(
    case: LiveSmokeCaseV1,
    *,
    host: PilotHostV1,
    authorized_input_root: Path,
    repository_root: Path,
) -> _LoadedSmokeFixtureV1:
    """Load one canonical, secret-free G1.5 fixture through a pinned fd."""

    path = Path(case.request_fixture_path)
    try:
        root = authorized_input_root.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ProductionDriverError(
            "SMOKE_FIXTURE_UNAVAILABLE", "fixture root is unavailable"
        ) from exc
    candidate = parent / path.name
    if (
        not root.is_dir()
        or _is_within_path(root, repository)
        or _is_within_path(repository, root)
        or not _is_within_path(candidate, root)
        or path.is_symlink()
    ):
        raise ProductionDriverError(
            "SMOKE_FIXTURE_PATH_REJECTED", "fixture is outside the authorized input root"
        )
    descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise ProductionDriverError(
                "NOFOLLOW_UNAVAILABLE", "fixture attestation is unavailable"
            )
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != case.request_fixture_byte_count
        ):
            raise ProductionDriverError("SMOKE_FIXTURE_MISMATCH", "fixture metadata differs")
        remaining = metadata.st_size
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                raise ProductionDriverError("SMOKE_FIXTURE_MISMATCH", "fixture is truncated")
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1) or digest.hexdigest() != case.request_fixture_sha256:
            raise ProductionDriverError("SMOKE_FIXTURE_MISMATCH", "fixture content differs")
        raw = b"".join(chunks)
    except OSError as exc:
        raise ProductionDriverError(
            "SMOKE_FIXTURE_UNAVAILABLE", "fixture cannot be opened"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value: object = json.loads(raw)
        if type(value) is not dict or canonical_json_bytes(cast(JsonValue, value)) != raw:
            raise ValueError("fixture is not canonical")
        fixture = cast(dict[str, object], value)
        if fixture.get("schema_version") != "mobileworld.g1.history-codec-captured-fixture/v1":
            raise ValueError("fixture schema differs")
        expected_codec = (
            "mobileworld.g1.history-codec.qwen-flat-progress"
            if host is PilotHostV1.QWEN3_VL
            else "mobileworld.g1.history-codec.mai-raw-replay"
        )
        if fixture.get("codec_id") != expected_codec:
            raise ValueError("fixture host differs")
        request_value = fixture.get("application_request")
        if type(request_value) is not dict:
            raise ValueError("application request is absent")
        request = request_value
        request_sha256 = canonical_sha256(cast(JsonValue, request))
        if fixture.get("fixture_request_sha256") != request_sha256:
            raise ValueError("inner request hash differs")
        task_instruction = _smoke_task_instruction(request, host=host)
        current_image_png = _smoke_current_image_png(request)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ProductionDriverError(
            "SMOKE_FIXTURE_SCHEMA_REJECTED", "fixture cannot enter the production runner"
        ) from exc
    return _LoadedSmokeFixtureV1(
        request=request,
        request_sha256=request_sha256,
        byte_count=len(raw),
        task_instruction=task_instruction,
        current_image_png=current_image_png,
    )


def _smoke_task_instruction(request: dict[str, JsonValue], *, host: PilotHostV1) -> str:
    messages = request.get("messages")
    if type(messages) is not list:
        raise ValueError("messages are absent")
    if host is PilotHostV1.MAI_UI:
        if len(messages) < 2 or type(messages[1]) is not dict:
            raise ValueError("MAI task region is absent")
        content = messages[1].get("content")
        if type(content) is not list or len(content) != 1 or type(content[0]) is not dict:
            raise ValueError("MAI task content differs")
        text_value = content[0].get("text")
        if type(text_value) is not str or not text_value.strip():
            raise ValueError("MAI task text is absent")
        return text_value.strip()
    candidates: list[str] = []
    for message in messages:
        if type(message) is not dict or message.get("role") != "user":
            continue
        content = message.get("content")
        if type(content) is not list:
            continue
        for block in content:
            if type(block) is not dict:
                continue
            text_value = block.get("text")
            if type(text_value) is not str:
                continue
            for line in text_value.splitlines():
                if line.strip().startswith("The user query:"):
                    candidate = line.strip().removeprefix("The user query:").strip()
                    if candidate:
                        candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError("Qwen task instruction is ambiguous")
    return candidates[0]


def _smoke_current_image_png(request: dict[str, JsonValue]) -> bytes:
    urls: list[str] = []
    messages = request.get("messages")
    if type(messages) is not list:
        raise ValueError("messages are absent")
    for message in messages:
        if type(message) is not dict:
            continue
        content = message.get("content")
        if type(content) is not list:
            continue
        for block in content:
            if type(block) is not dict or block.get("type") != "image_url":
                continue
            image_url = block.get("image_url")
            if type(image_url) is dict:
                url = image_url.get("url")
                if type(url) is str:
                    urls.append(url)
    if len(urls) != 1 or not urls[0].startswith("data:image/png;base64,"):
        raise ValueError("current image binding differs")
    import base64

    encoded = urls[0].removeprefix("data:image/png;base64,")
    raw = base64.b64decode(encoded, validate=True)
    if not raw or len(raw) > 40 * 1024 * 1024:
        raise ValueError("current image size differs")
    with Image.open(io.BytesIO(raw)) as image:
        image.verify()
    return raw


@dataclass(frozen=True, slots=True)
class ProductionRuntimeConfigV1:
    """Closed local-resource configuration; it carries no executable callback.

    The manifest supplies the model snapshots and actor endpoints.  This value
    supplies only host-local scheduling details that are absent from that
    contract.  Its complete projection is hash-bound before a production
    driver can be built.
    """

    backend_port: int
    backend_device: str
    qwen_gpu_index: int
    mai_gpu_index: int
    process_log_root: str
    authorized_pilot_input_root: str
    repository_root: str
    mobileworld_source_root: str
    vllm_python_executable: str
    vllm_python_realpath: str
    vllm_python_sha256: str
    vllm_python_byte_count: int
    vllm_version: str
    backend_image_id_sha256: str
    backend_environment_file: str
    backend_environment_file_device: int
    backend_environment_file_inode: int
    backend_environment_file_mode: int
    backend_environment_file_uid: int
    backend_environment_file_byte_count: int
    backend_environment_file_mtime_ns: int
    startup_timeout_seconds: int = 900
    shutdown_grace_seconds: int = 10
    health_poll_interval_ms: int = 250
    resource_topology: ProductionResourceTopologyV1 = (
        ProductionResourceTopologyV1.INDEPENDENT_GPU_CONCURRENT
    )
    vllm_gpu_memory_utilization: str = "0.90"
    minimum_free_gpu_memory_mib: int = 0

    def __post_init__(self) -> None:
        if type(self.backend_port) is not int or not 1_024 <= self.backend_port <= 65_535:
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG", "backend port is outside the unprivileged range"
            )
        if (
            type(self.backend_device) is not str
            or re.fullmatch(r"emulator-[0-9]{4,5}", self.backend_device) is None
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG", "backend device must be one exact emulator ID"
            )
        for gpu_value, name in (
            (self.qwen_gpu_index, "qwen_gpu_index"),
            (self.mai_gpu_index, "mai_gpu_index"),
        ):
            if type(gpu_value) is not int or not 0 <= gpu_value <= 255:
                raise ProductionDriverError(
                    "INVALID_RESOURCE_CONFIG", f"{name} is outside the closed GPU range"
                )
        if type(self.resource_topology) is not ProductionResourceTopologyV1:
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG", "resource topology must use the closed enum"
            )
        if (
            type(self.vllm_gpu_memory_utilization) is not str
            or re.fullmatch(r"0\.[0-9]{2}", self.vllm_gpu_memory_utilization) is None
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG", "vLLM GPU memory utilization must be exact decimal text"
            )
        if (
            type(self.minimum_free_gpu_memory_mib) is not int
            or not 0 <= self.minimum_free_gpu_memory_mib <= 1_048_576
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG", "minimum free GPU memory is outside its hard bound"
            )
        if self.resource_topology is ProductionResourceTopologyV1.INDEPENDENT_GPU_CONCURRENT:
            if (
                self.qwen_gpu_index == self.mai_gpu_index
                or self.vllm_gpu_memory_utilization != "0.90"
                or self.minimum_free_gpu_memory_mib != 0
            ):
                raise ProductionDriverError(
                    "INVALID_RESOURCE_CONFIG",
                    "concurrent topology requires distinct GPUs, utilization 0.90, and no floor",
                )
        elif (
            self.qwen_gpu_index != self.mai_gpu_index
            or self.vllm_gpu_memory_utilization != _SHARED_VLLM_GPU_MEMORY_UTILIZATION_V1
            or self.minimum_free_gpu_memory_mib != _SHARED_MINIMUM_FREE_GPU_MEMORY_MIB_V1
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG",
                "shared sequential topology requires one GPU, utilization 0.24, and 51200 MiB free",
            )
        for path_value, name in (
            (self.process_log_root, "process_log_root"),
            (self.authorized_pilot_input_root, "authorized_pilot_input_root"),
            (self.repository_root, "repository_root"),
            (self.mobileworld_source_root, "mobileworld_source_root"),
            (self.vllm_python_executable, "vllm_python_executable"),
            (self.vllm_python_realpath, "vllm_python_realpath"),
            (self.backend_environment_file, "backend_environment_file"),
        ):
            if type(path_value) is not str or not path_value or not Path(path_value).is_absolute():
                raise ProductionDriverError(
                    "INVALID_RESOURCE_CONFIG", f"{name} must be an absolute path"
                )
        _require_sha256(self.vllm_python_sha256, "vllm_python_sha256")
        _require_sha256(self.backend_image_id_sha256, "backend_image_id_sha256")
        expected_source_root = Path(self.repository_root) / "MobileWorld" / "src"
        if Path(os.path.normpath(self.mobileworld_source_root)) != expected_source_root:
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG",
                "MobileWorld source root must be the current repository source tree",
            )
        repository = Path(self.repository_root).resolve(strict=False)
        for path_value, name in (
            (self.process_log_root, "process_log_root"),
            (self.authorized_pilot_input_root, "authorized_pilot_input_root"),
            (self.backend_environment_file, "backend_environment_file"),
        ):
            candidate = Path(path_value).resolve(strict=False)
            if (
                candidate == repository
                or candidate.is_relative_to(repository)
                or repository.is_relative_to(candidate)
            ):
                raise ProductionDriverError(
                    "INVALID_RESOURCE_CONFIG", f"{name} must not overlap the repository"
                )
        if (
            type(self.vllm_python_byte_count) is not int
            or not 1 <= self.vllm_python_byte_count <= 1_000_000_000
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG", "vLLM Python byte count is outside its hard bound"
            )
        if (
            type(self.backend_environment_file_byte_count) is not int
            or not 1 <= self.backend_environment_file_byte_count <= 1_048_576
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG",
                "backend environment byte count is outside its hard bound",
            )
        for value, name in (
            (self.backend_environment_file_device, "backend_environment_file_device"),
            (self.backend_environment_file_inode, "backend_environment_file_inode"),
            (self.backend_environment_file_uid, "backend_environment_file_uid"),
            (self.backend_environment_file_mtime_ns, "backend_environment_file_mtime_ns"),
        ):
            if type(value) is not int or value < 0:
                raise ProductionDriverError(
                    "INVALID_RESOURCE_CONFIG", f"{name} must be nonnegative"
                )
        if self.backend_environment_file_mode != 0o600:
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG", "backend environment file mode must be 0600"
            )
        if (
            type(self.vllm_version) is not str
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]{0,32})?", self.vllm_version)
            is None
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_CONFIG", "vLLM version must be explicitly pinned"
            )
        for value, name, maximum in (
            (self.startup_timeout_seconds, "startup_timeout_seconds", 3_600),
            (self.shutdown_grace_seconds, "shutdown_grace_seconds", 60),
            (self.health_poll_interval_ms, "health_poll_interval_ms", 5_000),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ProductionDriverError(
                    "INVALID_RESOURCE_CONFIG", f"{name} is outside its hard bound"
                )
        if (
            self.shutdown_grace_seconds * 1_000_000_000
            <= _PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1
        ):
            raise ProductionDriverError(
                "INSUFFICIENT_SHUTDOWN_GRACE",
                "shutdown grace must exceed the sealed attempt termination bound",
            )


def production_runtime_config_projection(
    value: ProductionRuntimeConfigV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionRuntimeConfigV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "runtime config type differs")
    trusted = ProductionRuntimeConfigV1(
        backend_port=value.backend_port,
        backend_device=value.backend_device,
        qwen_gpu_index=value.qwen_gpu_index,
        mai_gpu_index=value.mai_gpu_index,
        process_log_root=value.process_log_root,
        authorized_pilot_input_root=value.authorized_pilot_input_root,
        repository_root=value.repository_root,
        mobileworld_source_root=value.mobileworld_source_root,
        vllm_python_executable=value.vllm_python_executable,
        vllm_python_realpath=value.vllm_python_realpath,
        vllm_python_sha256=value.vllm_python_sha256,
        vllm_python_byte_count=value.vllm_python_byte_count,
        vllm_version=value.vllm_version,
        backend_image_id_sha256=value.backend_image_id_sha256,
        backend_environment_file=value.backend_environment_file,
        backend_environment_file_device=value.backend_environment_file_device,
        backend_environment_file_inode=value.backend_environment_file_inode,
        backend_environment_file_mode=value.backend_environment_file_mode,
        backend_environment_file_uid=value.backend_environment_file_uid,
        backend_environment_file_byte_count=value.backend_environment_file_byte_count,
        backend_environment_file_mtime_ns=value.backend_environment_file_mtime_ns,
        startup_timeout_seconds=value.startup_timeout_seconds,
        shutdown_grace_seconds=value.shutdown_grace_seconds,
        health_poll_interval_ms=value.health_poll_interval_ms,
        resource_topology=value.resource_topology,
        vllm_gpu_memory_utilization=value.vllm_gpu_memory_utilization,
        minimum_free_gpu_memory_mib=value.minimum_free_gpu_memory_mib,
    )
    return {
        "authorized_pilot_input_root": trusted.authorized_pilot_input_root,
        "backend_device": trusted.backend_device,
        "backend_image": _MOBILEWORLD_BACKEND_IMAGE,
        "backend_image_id_sha256": trusted.backend_image_id_sha256,
        "backend_environment_file": trusted.backend_environment_file,
        "backend_environment_file_byte_count": trusted.backend_environment_file_byte_count,
        "backend_environment_file_device": trusted.backend_environment_file_device,
        "backend_environment_file_inode": trusted.backend_environment_file_inode,
        "backend_environment_file_mode": trusted.backend_environment_file_mode,
        "backend_environment_file_mtime_ns": trusted.backend_environment_file_mtime_ns,
        "backend_environment_file_uid": trusted.backend_environment_file_uid,
        "backend_network": _DOCKER_NETWORK,
        "backend_port": trusted.backend_port,
        "health_poll_interval_ms": trusted.health_poll_interval_ms,
        "mai_gpu_index": trusted.mai_gpu_index,
        "minimum_free_gpu_memory_mib": trusted.minimum_free_gpu_memory_mib,
        "mobileworld_source_mount": "/app/service/src",
        "mobileworld_source_root": trusted.mobileworld_source_root,
        "process_log_root": trusted.process_log_root,
        "qwen_gpu_index": trusted.qwen_gpu_index,
        "repository_root": trusted.repository_root,
        "resource_topology": trusted.resource_topology.value,
        "shutdown_grace_seconds": trusted.shutdown_grace_seconds,
        "startup_timeout_seconds": trusted.startup_timeout_seconds,
        "vllm_max_batched_tokens": _VLLM_MAX_BATCHED_TOKENS,
        "vllm_gpu_memory_utilization": trusted.vllm_gpu_memory_utilization,
        "vllm_max_model_len": _VLLM_MAX_MODEL_LEN,
        "vllm_python_byte_count": trusted.vllm_python_byte_count,
        "vllm_python_executable": trusted.vllm_python_executable,
        "vllm_python_realpath": trusted.vllm_python_realpath,
        "vllm_python_sha256": trusted.vllm_python_sha256,
        "vllm_version": trusted.vllm_version,
    }


def production_runtime_config_sha256(value: ProductionRuntimeConfigV1) -> str:
    return _hash_projection(
        "production-runtime-config",
        cast(JsonValue, production_runtime_config_projection(value)),
    )


def _production_resource_cleanup_bound_projection(
    config: ProductionRuntimeConfigV1,
) -> dict[str, JsonValue]:
    """Return the complete bounded-wait budget for one shared cleanup call."""

    if type(config) is not ProductionRuntimeConfigV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "cleanup bound config type differs")
    if config.resource_topology is not ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED:
        raise ProductionDriverError(
            "CLEANUP_BOUND_UNAVAILABLE",
            "the sealed cleanup bound applies only to shared sequential resources",
        )
    poll_ceiling_seconds = (config.health_poll_interval_ms + 999) // 1_000
    admitted_model_seconds = 5 * config.shutdown_grace_seconds + 3 * poll_ceiling_seconds
    partial_model_seconds = 3 * config.shutdown_grace_seconds + 2 * poll_ceiling_seconds
    model_seconds = max(admitted_model_seconds, partial_model_seconds)
    admitted_backend_seconds = max(
        3 * _RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        config.shutdown_grace_seconds + 2 * _RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
    )
    pending_backend_seconds = 7 * _RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1
    backend_seconds = max(admitted_backend_seconds, pending_backend_seconds)
    final_shared_gpu_attestation_seconds = 4 * _RESOURCE_ATTESTATION_COMMAND_TIMEOUT_SECONDS_V1
    cleanup_upper_bound_seconds = (
        model_seconds + backend_seconds + final_shared_gpu_attestation_seconds
    )
    return {
        "admitted_backend_cleanup_upper_bound_seconds": admitted_backend_seconds,
        "admitted_model_cleanup_upper_bound_seconds": admitted_model_seconds,
        "backend_cleanup_upper_bound_seconds": backend_seconds,
        "cleanup_upper_bound_seconds": cleanup_upper_bound_seconds,
        "docker_command_timeout_seconds": _RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        "final_shared_gpu_attestation_command_slots": 4,
        "final_shared_gpu_attestation_upper_bound_seconds": (final_shared_gpu_attestation_seconds),
        "health_poll_interval_ceiling_seconds": poll_ceiling_seconds,
        "health_poll_interval_ms": config.health_poll_interval_ms,
        "model_cleanup_upper_bound_seconds": model_seconds,
        "model_leader_wait_slots": 2,
        "model_poll_overshoot_slots": 3,
        "model_port_wait_slots": 1,
        "model_session_wait_slots": 2,
        "nvidia_attestation_command_timeout_seconds": (
            _RESOURCE_ATTESTATION_COMMAND_TIMEOUT_SECONDS_V1
        ),
        "partial_model_cleanup_upper_bound_seconds": partial_model_seconds,
        "pending_backend_cleanup_command_slots": 7,
        "pending_backend_cleanup_upper_bound_seconds": pending_backend_seconds,
        "resource_topology": config.resource_topology.value,
        "runtime_config_sha256": production_runtime_config_sha256(config),
        "shutdown_grace_seconds": config.shutdown_grace_seconds,
    }


def _production_resource_cleanup_bound_preimage(config: ProductionRuntimeConfigV1) -> bytes:
    return canonical_json_bytes(
        cast(
            JsonValue,
            {
                "domain": "production-resource-cleanup-bound",
                "schema_version": PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1,
                "value": _production_resource_cleanup_bound_projection(config),
            },
        )
    )


def parse_production_runtime_config(value: JsonValue) -> ProductionRuntimeConfigV1:
    """Parse the exact canonical projection used by the production CLI."""

    if type(value) is not dict:
        raise ProductionDriverError("INVALID_RESOURCE_CONFIG", "runtime config is not an object")
    mapping = value
    expected = {
        "authorized_pilot_input_root",
        "backend_device",
        "backend_image",
        "backend_image_id_sha256",
        "backend_environment_file",
        "backend_environment_file_byte_count",
        "backend_environment_file_device",
        "backend_environment_file_inode",
        "backend_environment_file_mode",
        "backend_environment_file_mtime_ns",
        "backend_environment_file_uid",
        "backend_network",
        "backend_port",
        "health_poll_interval_ms",
        "mai_gpu_index",
        "minimum_free_gpu_memory_mib",
        "mobileworld_source_mount",
        "mobileworld_source_root",
        "process_log_root",
        "qwen_gpu_index",
        "repository_root",
        "resource_topology",
        "shutdown_grace_seconds",
        "startup_timeout_seconds",
        "vllm_max_batched_tokens",
        "vllm_gpu_memory_utilization",
        "vllm_max_model_len",
        "vllm_python_byte_count",
        "vllm_python_executable",
        "vllm_python_realpath",
        "vllm_python_sha256",
        "vllm_version",
    }
    if set(mapping) != expected:
        raise ProductionDriverError("INVALID_RESOURCE_CONFIG", "runtime config keys differ")
    if (
        mapping["backend_image"] != _MOBILEWORLD_BACKEND_IMAGE
        or mapping["backend_network"] != _DOCKER_NETWORK
        or mapping["mobileworld_source_mount"] != "/app/service/src"
        or mapping["vllm_max_batched_tokens"] != _VLLM_MAX_BATCHED_TOKENS
        or mapping["vllm_max_model_len"] != _VLLM_MAX_MODEL_LEN
    ):
        raise ProductionDriverError("INVALID_RESOURCE_CONFIG", "fixed runtime constants differ")
    try:
        return ProductionRuntimeConfigV1(
            backend_port=cast(int, mapping["backend_port"]),
            backend_device=cast(str, mapping["backend_device"]),
            qwen_gpu_index=cast(int, mapping["qwen_gpu_index"]),
            mai_gpu_index=cast(int, mapping["mai_gpu_index"]),
            process_log_root=cast(str, mapping["process_log_root"]),
            authorized_pilot_input_root=cast(str, mapping["authorized_pilot_input_root"]),
            repository_root=cast(str, mapping["repository_root"]),
            mobileworld_source_root=cast(str, mapping["mobileworld_source_root"]),
            vllm_python_executable=cast(str, mapping["vllm_python_executable"]),
            vllm_python_realpath=cast(str, mapping["vllm_python_realpath"]),
            vllm_python_sha256=cast(str, mapping["vllm_python_sha256"]),
            vllm_python_byte_count=cast(int, mapping["vllm_python_byte_count"]),
            vllm_version=cast(str, mapping["vllm_version"]),
            backend_image_id_sha256=cast(str, mapping["backend_image_id_sha256"]),
            backend_environment_file=cast(str, mapping["backend_environment_file"]),
            backend_environment_file_device=cast(int, mapping["backend_environment_file_device"]),
            backend_environment_file_inode=cast(int, mapping["backend_environment_file_inode"]),
            backend_environment_file_mode=cast(int, mapping["backend_environment_file_mode"]),
            backend_environment_file_uid=cast(int, mapping["backend_environment_file_uid"]),
            backend_environment_file_byte_count=cast(
                int, mapping["backend_environment_file_byte_count"]
            ),
            backend_environment_file_mtime_ns=cast(
                int, mapping["backend_environment_file_mtime_ns"]
            ),
            startup_timeout_seconds=cast(int, mapping["startup_timeout_seconds"]),
            shutdown_grace_seconds=cast(int, mapping["shutdown_grace_seconds"]),
            health_poll_interval_ms=cast(int, mapping["health_poll_interval_ms"]),
            resource_topology=ProductionResourceTopologyV1(cast(str, mapping["resource_topology"])),
            vllm_gpu_memory_utilization=cast(str, mapping["vllm_gpu_memory_utilization"]),
            minimum_free_gpu_memory_mib=cast(int, mapping["minimum_free_gpu_memory_mib"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ProductionDriverError):
            raise
        raise ProductionDriverError(
            "INVALID_RESOURCE_CONFIG", "runtime config field types differ"
        ) from exc


@dataclass(frozen=True, slots=True)
class ProductionCommandSpecV1:
    """Hashable, fixed-argv process declaration with a sanitized environment."""

    kind: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    endpoint: str
    health_paths: tuple[str, ...]
    expected_model_id: str | None

    def __post_init__(self) -> None:
        if self.kind not in {"VLLM_QWEN", "VLLM_MAI", "MOBILEWORLD_BACKEND"}:
            raise ProductionDriverError("INVALID_COMMAND_SPEC", "unknown command kind")
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(type(item) is not str or not item or "\x00" in item for item in self.argv)
        ):
            raise ProductionDriverError("INVALID_COMMAND_SPEC", "argv is not an exact tuple")
        raw_environment: object = self.environment
        if type(raw_environment) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or any(type(part) is not str or "\x00" in part for part in item)
            for item in cast(tuple[object, ...], raw_environment)
        ):
            raise ProductionDriverError(
                "INVALID_COMMAND_SPEC", "environment is not an exact pair tuple"
            )
        if tuple(sorted(self.environment)) != self.environment or len(
            {key for key, _ in self.environment}
        ) != len(self.environment):
            raise ProductionDriverError(
                "INVALID_COMMAND_SPEC", "environment keys must be unique and sorted"
            )
        if type(self.health_paths) is not tuple or not self.health_paths:
            raise ProductionDriverError("INVALID_COMMAND_SPEC", "health paths are absent")
        for path in self.health_paths:
            if type(path) is not str or not path.startswith("/") or "?" in path:
                raise ProductionDriverError("INVALID_COMMAND_SPEC", "health path is invalid")


def production_command_spec_projection(value: ProductionCommandSpecV1) -> dict[str, JsonValue]:
    if type(value) is not ProductionCommandSpecV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "command spec type differs")
    return {
        "argv": list(value.argv),
        "endpoint": value.endpoint,
        "environment": [{"key": key, "value": item} for key, item in value.environment],
        "expected_model_id": value.expected_model_id,
        "health_paths": list(value.health_paths),
        "kind": value.kind,
        "shell": False,
    }


def production_command_spec_sha256(value: ProductionCommandSpecV1) -> str:
    return _hash_projection(
        "production-command-spec", cast(JsonValue, production_command_spec_projection(value))
    )


def _exact_loopback_endpoint(
    value: str, *, permitted_paths: frozenset[str]
) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProductionDriverError("INVALID_LOOPBACK_ENDPOINT", "endpoint is malformed") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1_024 <= port <= 65_535
        or parsed.path not in permitted_paths
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProductionDriverError(
            "INVALID_LOOPBACK_ENDPOINT", "production endpoint must be exact IPv4 loopback"
        )
    return parsed.hostname, port, parsed.path


def _vllm_command_spec(
    resource: SnapshotResourceV1,
    *,
    gpu_index: int,
    config: ProductionRuntimeConfigV1,
) -> ProductionCommandSpecV1:
    host, port, _ = _exact_loopback_endpoint(
        resource.actor_endpoint, permitted_paths=frozenset({"", "/v1"})
    )
    kind = "VLLM_QWEN" if resource.host is PilotHostV1.QWEN3_VL else "VLLM_MAI"
    argv = (
        config.vllm_python_executable,
        "-P",
        "-B",
        "-m",
        "vllm.entrypoints.cli.main",
        "serve",
        resource.snapshot_path,
        "--served-model-name",
        resource.served_model_id,
        "--host",
        host,
        "--port",
        str(port),
        "--dtype",
        "bfloat16",
        "--max-model-len",
        str(_VLLM_MAX_MODEL_LEN),
        "--enforce-eager",
        "--gpu-memory-utilization",
        config.vllm_gpu_memory_utilization,
        "--limit-mm-per-prompt",
        '{"image":3,"video":0}',
        "--mm-processor-cache-gb",
        "1",
        "--max-num-batched-tokens",
        str(_VLLM_MAX_BATCHED_TOKENS),
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
    environment = tuple(
        sorted(
            {
                "CUDA_VISIBLE_DEVICES": str(gpu_index),
                "DO_NOT_TRACK": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "HF_HUB_OFFLINE": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "VLLM_NO_USAGE_STATS": "1",
                "no_proxy": "127.0.0.1,localhost",
            }.items()
        )
    )
    return ProductionCommandSpecV1(
        kind=kind,
        argv=argv,
        environment=environment,
        endpoint=f"http://{host}:{port}",
        health_paths=("/health", "/v1/models"),
        expected_model_id=resource.served_model_id,
    )


def _backend_command_spec(
    config: ProductionRuntimeConfigV1,
    *,
    manifest_sha256: str,
) -> ProductionCommandSpecV1:
    name = f"r24-{manifest_sha256[:20]}"
    endpoint = f"http://127.0.0.1:{config.backend_port}"
    return ProductionCommandSpecV1(
        kind="MOBILEWORLD_BACKEND",
        argv=(
            _DOCKER_EXECUTABLE,
            "run",
            "--detach",
            "--rm",
            "--privileged",
            "--name",
            name,
            "--network",
            _DOCKER_NETWORK,
            "--env-file",
            config.backend_environment_file,
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--volume",
            f"{config.mobileworld_source_root}:/app/service/src:ro",
            "--publish",
            f"127.0.0.1:{config.backend_port}:{_MOBILEWORLD_BACKEND_CONTAINER_PORT}",
            _MOBILEWORLD_BACKEND_IMAGE,
        ),
        environment=(),
        endpoint=endpoint,
        health_paths=("/health",),
        expected_model_id=None,
    )


@dataclass(frozen=True, slots=True)
class OwnedProcessIdentityV1:
    pid: int
    process_group_id: int
    session_id: int
    starttime_ticks: int
    uid: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.pid, "pid"),
            (self.process_group_id, "process_group_id"),
            (self.session_id, "session_id"),
            (self.starttime_ticks, "starttime_ticks"),
            (self.uid, "uid"),
        ):
            if type(value) is not int or value < 0:
                raise ProductionDriverError(
                    "INVALID_PROCESS_IDENTITY", f"{name} must be nonnegative"
                )
        if self.pid < 2 or self.process_group_id != self.pid or self.session_id != self.pid:
            raise ProductionDriverError(
                "INVALID_PROCESS_IDENTITY", "model server must own a new PID/session/process group"
            )


@dataclass(frozen=True, slots=True)
class SharedGpuProcessEvidenceV1:
    """One compute tenant observed on the shared physical GPU."""

    pid: int
    starttime_ticks: int
    process_group_id: int
    session_id: int
    uid: int
    user: str
    used_gpu_memory_mib: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.pid, "pid"),
            (self.starttime_ticks, "starttime_ticks"),
            (self.process_group_id, "process_group_id"),
            (self.session_id, "session_id"),
            (self.uid, "uid"),
            (self.used_gpu_memory_mib, "used_gpu_memory_mib"),
        ):
            if type(value) is not int or value < 0:
                raise ProductionDriverError(
                    "INVALID_GPU_ATTESTATION", f"{name} must be nonnegative"
                )
        if self.pid < 2 or self.process_group_id < 1 or self.session_id < 1:
            raise ProductionDriverError(
                "INVALID_GPU_ATTESTATION", "shared compute process identity differs"
            )
        if type(self.user) is not str or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.user) is None:
            raise ProductionDriverError(
                "INVALID_GPU_ATTESTATION", "shared compute process user differs"
            )


def _shared_gpu_process_projection(value: SharedGpuProcessEvidenceV1) -> dict[str, JsonValue]:
    return {
        "pid": value.pid,
        "process_group_id": value.process_group_id,
        "session_id": value.session_id,
        "starttime_ticks": value.starttime_ticks,
        "uid": value.uid,
        "used_gpu_memory_mib": value.used_gpu_memory_mib,
        "user": value.user,
    }


@dataclass(frozen=True, slots=True)
class ProductionSharedGpuAttestationV1:
    """Detached capacity and co-tenant snapshot for controlled GPU sharing."""

    gpu_index: int
    gpu_uuid: str
    total_memory_mib: int
    free_memory_mib: int
    used_memory_mib: int
    reserved_memory_mib: int
    gpu_utilization_percent: int
    memory_utilization_percent: int
    minimum_free_memory_mib: int
    processes: tuple[SharedGpuProcessEvidenceV1, ...]

    def __post_init__(self) -> None:
        if type(self.gpu_index) is not int or not 0 <= self.gpu_index <= 255:
            raise ProductionDriverError("INVALID_GPU_ATTESTATION", "GPU index differs")
        if type(self.gpu_uuid) is not str or _NVIDIA_GPU_UUID.fullmatch(self.gpu_uuid) is None:
            raise ProductionDriverError("INVALID_GPU_ATTESTATION", "GPU UUID differs")
        for value, name in (
            (self.total_memory_mib, "total_memory_mib"),
            (self.free_memory_mib, "free_memory_mib"),
            (self.used_memory_mib, "used_memory_mib"),
            (self.reserved_memory_mib, "reserved_memory_mib"),
            (self.gpu_utilization_percent, "gpu_utilization_percent"),
            (self.memory_utilization_percent, "memory_utilization_percent"),
            (self.minimum_free_memory_mib, "minimum_free_memory_mib"),
        ):
            if type(value) is not int or value < 0:
                raise ProductionDriverError(
                    "INVALID_GPU_ATTESTATION", f"{name} must be nonnegative"
                )
        if (
            self.total_memory_mib == 0
            or abs(
                self.total_memory_mib
                - self.free_memory_mib
                - self.used_memory_mib
                - self.reserved_memory_mib
            )
            > _NVIDIA_MEMORY_ACCOUNTING_TOLERANCE_MIB_V1
            or self.free_memory_mib + self.used_memory_mib
            > self.total_memory_mib + _NVIDIA_MEMORY_ACCOUNTING_TOLERANCE_MIB_V1
            or self.minimum_free_memory_mib > self.total_memory_mib
            or self.gpu_utilization_percent > 100
            or self.memory_utilization_percent > 100
        ):
            raise ProductionDriverError("INVALID_GPU_ATTESTATION", "GPU memory census differs")
        if type(self.processes) is not tuple or any(
            type(item) is not SharedGpuProcessEvidenceV1 for item in self.processes
        ):
            raise ProductionDriverError("INVALID_GPU_ATTESTATION", "process census differs")
        identities = tuple((item.pid, item.starttime_ticks) for item in self.processes)
        if len(identities) != len(set(identities)):
            raise ProductionDriverError("INVALID_GPU_ATTESTATION", "process identity is duplicated")


def production_shared_gpu_attestation_projection(
    value: ProductionSharedGpuAttestationV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionSharedGpuAttestationV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "shared GPU attestation type differs")
    return {
        "free_memory_mib": value.free_memory_mib,
        "gpu_utilization_percent": value.gpu_utilization_percent,
        "gpu_index": value.gpu_index,
        "gpu_uuid": value.gpu_uuid,
        "minimum_free_memory_mib": value.minimum_free_memory_mib,
        "memory_utilization_percent": value.memory_utilization_percent,
        "processes": [_shared_gpu_process_projection(item) for item in value.processes],
        "reserved_memory_mib": value.reserved_memory_mib,
        "total_memory_mib": value.total_memory_mib,
        "used_memory_mib": value.used_memory_mib,
    }


def production_shared_gpu_attestation_sha256(value: ProductionSharedGpuAttestationV1) -> str:
    return _hash_projection(
        "production-shared-gpu-attestation",
        cast(JsonValue, production_shared_gpu_attestation_projection(value)),
    )


def _require_shared_gpu_capacity(value: ProductionSharedGpuAttestationV1) -> None:
    if value.free_memory_mib < value.minimum_free_memory_mib:
        raise ProductionDriverError(
            "GPU_SHARED_CAPACITY_INSUFFICIENT",
            "shared GPU free memory is below the hash-bound minimum",
        )


def _validate_shared_gpu_tenants(
    value: ProductionSharedGpuAttestationV1,
    *,
    baseline: ProductionSharedGpuAttestationV1,
    owned_identity: OwnedProcessIdentityV1 | None,
) -> None:
    if value.gpu_index != baseline.gpu_index or value.gpu_uuid != baseline.gpu_uuid:
        raise ProductionDriverError("GPU_IDENTITY_MISMATCH", "shared physical GPU drifted")
    baseline_identities = {
        (item.pid, item.starttime_ticks, item.uid) for item in baseline.processes
    }
    owned_process_seen = False
    for item in value.processes:
        identity = (item.pid, item.starttime_ticks, item.uid)
        if identity in baseline_identities:
            continue
        if (
            owned_identity is not None
            and item.uid == owned_identity.uid
            and item.session_id == owned_identity.session_id
        ):
            owned_process_seen = True
            continue
        raise ProductionDriverError(
            "GPU_SHARED_TENANT_DRIFT",
            "a new non-owned compute process appeared on the shared GPU",
        )
    if owned_identity is not None and not owned_process_seen:
        raise ProductionDriverError(
            "GPU_OWNED_PROCESS_ABSENT",
            "the owned model session has no bound compute process on the shared GPU",
        )


def _validate_shared_gpu_cleanup(
    value: ProductionSharedGpuAttestationV1,
    *,
    baseline: ProductionSharedGpuAttestationV1,
    stopped_models: tuple[ProductionModelStopEvidenceV1, ...],
) -> None:
    if value.gpu_index != baseline.gpu_index or value.gpu_uuid != baseline.gpu_uuid:
        raise ProductionDriverError("GPU_IDENTITY_MISMATCH", "shared physical GPU drifted")
    stopped_sessions = {(item.process.uid, item.process.session_id) for item in stopped_models}
    stopped_identities = {
        (item.process.pid, item.process.starttime_ticks, item.process.uid)
        for item in stopped_models
    }
    if any(
        (item.uid, item.session_id) in stopped_sessions
        or (item.pid, item.starttime_ticks, item.uid) in stopped_identities
        for item in value.processes
    ):
        raise ProductionDriverError(
            "MODEL_REAP_UNCONFIRMED",
            "an owned model identity remains in the final shared GPU census",
        )


@dataclass(frozen=True, slots=True)
class ProductionResourceStageEvidenceV1:
    manifest_sha256: str
    runtime_config_sha256: str
    runtime_attestation_sha256: str
    backend_command_sha256: str
    backend_container_id: str
    backend_health_sha256: str
    model_command_sha256s: tuple[str, str]
    model_processes: tuple[OwnedProcessIdentityV1, ...]
    model_health_sha256s: tuple[str, ...]
    gpu_lease_sha256s: tuple[str, ...]
    gpu_idle_attestation_sha256s: tuple[str, ...]
    shared_gpu_attestations: tuple[ProductionSharedGpuAttestationV1, ...]
    active_hosts: tuple[PilotHostV1, ...]
    resource_topology: ProductionResourceTopologyV1
    vllm_gpu_memory_utilization: str
    minimum_free_gpu_memory_mib: int
    sequence_execution_scope: str
    sequence_scope_authority_sha256: str

    def __post_init__(self) -> None:
        if type(self.resource_topology) is not ProductionResourceTopologyV1:
            raise ProductionDriverError(
                "INVALID_RESOURCE_EVIDENCE", "resource topology type differs"
            )
        for name in (
            "manifest_sha256",
            "runtime_config_sha256",
            "runtime_attestation_sha256",
            "backend_command_sha256",
            "backend_health_sha256",
            "sequence_scope_authority_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if (
            type(self.backend_container_id) is not str
            or _SHA256.fullmatch(self.backend_container_id) is None
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_EVIDENCE", "backend container ID is not exact"
            )
        raw_commands: object = self.model_command_sha256s
        raw_health: object = self.model_health_sha256s
        expected_active_count = (
            1
            if self.resource_topology is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
            else 2
        )
        if (
            type(raw_commands) is not tuple
            or len(raw_commands) != 2
            or type(raw_health) is not tuple
            or len(raw_health) != expected_active_count
            or any(
                _SHA256.fullmatch(item) is None
                for item in (*self.model_command_sha256s, *self.model_health_sha256s)
            )
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_EVIDENCE", "model evidence matrix differs"
            )
        raw_processes: object = self.model_processes
        raw_leases: object = self.gpu_lease_sha256s
        raw_gpu_idle: object = self.gpu_idle_attestation_sha256s
        if (
            type(raw_processes) is not tuple
            or len(raw_processes) != expected_active_count
            or any(type(item) is not OwnedProcessIdentityV1 for item in self.model_processes)
            or type(raw_leases) is not tuple
            or len(raw_leases) != expected_active_count
            or any(
                type(item) is not str or _SHA256.fullmatch(item) is None
                for item in cast(tuple[object, ...], raw_leases)
            )
            or type(raw_gpu_idle) is not tuple
            or len(raw_gpu_idle)
            != (
                0
                if self.resource_topology
                is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
                else 2
            )
            or any(
                type(item) is not str or _SHA256.fullmatch(item) is None
                for item in cast(tuple[object, ...], raw_gpu_idle)
            )
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_EVIDENCE", "model process identity matrix differs"
            )
        if (
            type(self.active_hosts) is not tuple
            or len(self.active_hosts) != expected_active_count
            or any(type(item) is not PilotHostV1 for item in self.active_hosts)
            or tuple(dict.fromkeys(self.active_hosts)) != self.active_hosts
            or type(self.shared_gpu_attestations) is not tuple
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_EVIDENCE", "active-host or shared-GPU evidence differs"
            )
        if self.resource_topology is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED:
            if (
                self.active_hosts != (PilotHostV1.QWEN3_VL,)
                or len(self.shared_gpu_attestations) != 2
                or any(
                    type(item) is not ProductionSharedGpuAttestationV1
                    for item in self.shared_gpu_attestations
                )
                or self.vllm_gpu_memory_utilization != _SHARED_VLLM_GPU_MEMORY_UTILIZATION_V1
                or self.minimum_free_gpu_memory_mib != _SHARED_MINIMUM_FREE_GPU_MEMORY_MIB_V1
                or self.sequence_execution_scope != "R24_LIVE_SMOKE_ONLY"
            ):
                raise ProductionDriverError(
                    "INVALID_RESOURCE_EVIDENCE", "shared sequential resource evidence differs"
                )
            _require_sha256(
                self.sequence_scope_authority_sha256,
                "sequence_scope_authority_sha256",
            )
            baseline, ready = self.shared_gpu_attestations
            _require_shared_gpu_capacity(baseline)
            if (
                baseline.minimum_free_memory_mib != self.minimum_free_gpu_memory_mib
                or ready.minimum_free_memory_mib != 0
                or baseline.gpu_index != ready.gpu_index
                or baseline.gpu_uuid != ready.gpu_uuid
            ):
                raise ProductionDriverError(
                    "INVALID_RESOURCE_EVIDENCE", "shared GPU attestation sequence differs"
                )
            _validate_shared_gpu_tenants(
                ready,
                baseline=baseline,
                owned_identity=self.model_processes[0],
            )
        elif (
            self.active_hosts != (PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI)
            or self.shared_gpu_attestations
            or self.vllm_gpu_memory_utilization != "0.90"
            or self.minimum_free_gpu_memory_mib != 0
            or self.sequence_execution_scope != "R24_R25_FULL"
        ):
            raise ProductionDriverError(
                "INVALID_RESOURCE_EVIDENCE", "concurrent resource evidence differs"
            )


def production_resource_stage_evidence_projection(
    value: ProductionResourceStageEvidenceV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionResourceStageEvidenceV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "resource evidence type differs")
    projection: dict[str, JsonValue] = {
        "backend_command_sha256": value.backend_command_sha256,
        "backend_container_id": value.backend_container_id,
        "backend_health_sha256": value.backend_health_sha256,
        "manifest_sha256": value.manifest_sha256,
        "gpu_lease_sha256s": list(value.gpu_lease_sha256s),
        "gpu_idle_attestation_sha256s": list(value.gpu_idle_attestation_sha256s),
        "model_command_sha256s": list(value.model_command_sha256s),
        "model_health_sha256s": list(value.model_health_sha256s),
        "model_processes": [
            {
                "pid": item.pid,
                "process_group_id": item.process_group_id,
                "session_id": item.session_id,
                "starttime_ticks": item.starttime_ticks,
                "uid": item.uid,
            }
            for item in value.model_processes
        ],
        "runtime_config_sha256": value.runtime_config_sha256,
        "runtime_attestation_sha256": value.runtime_attestation_sha256,
    }
    if value.resource_topology is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED:
        projection.update(
            {
                "active_hosts": [item.value for item in value.active_hosts],
                "minimum_free_gpu_memory_mib": value.minimum_free_gpu_memory_mib,
                "resource_topology": value.resource_topology.value,
                "shared_gpu_attestation_sha256s": [
                    production_shared_gpu_attestation_sha256(item)
                    for item in value.shared_gpu_attestations
                ],
                "shared_gpu_attestations": [
                    production_shared_gpu_attestation_projection(item)
                    for item in value.shared_gpu_attestations
                ],
                "vllm_gpu_memory_utilization": value.vllm_gpu_memory_utilization,
                "sequence_execution_scope": value.sequence_execution_scope,
                "sequence_scope_authority_sha256": value.sequence_scope_authority_sha256,
            }
        )
    return projection


def production_resource_stage_evidence_sha256(
    value: ProductionResourceStageEvidenceV1,
) -> str:
    schema_version = (
        PRODUCTION_SHARED_RESOURCE_EVIDENCE_SCHEMA_VERSION_V2
        if value.resource_topology is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        else PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION
    )
    return canonical_sha256(
        cast(
            JsonValue,
            {
                "domain": "production-resource-stage-evidence",
                "schema_version": schema_version,
                "value": production_resource_stage_evidence_projection(value),
            },
        )
    )


def _production_resource_stage_evidence_preimage(
    value: ProductionResourceStageEvidenceV1,
) -> bytes:
    schema_version = (
        PRODUCTION_SHARED_RESOURCE_EVIDENCE_SCHEMA_VERSION_V2
        if value.resource_topology is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        else PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION
    )
    return canonical_json_bytes(
        cast(
            JsonValue,
            {
                "domain": "production-resource-stage-evidence",
                "schema_version": schema_version,
                "value": production_resource_stage_evidence_projection(value),
            },
        )
    )


@dataclass(frozen=True, slots=True)
class ProductionModelStopEvidenceV1:
    host: PilotHostV1
    process: OwnedProcessIdentityV1
    endpoint: str
    leader_reaped: bool
    session_members_remaining: int
    port_available: bool

    def __post_init__(self) -> None:
        if type(self.host) is not PilotHostV1 or type(self.process) is not OwnedProcessIdentityV1:
            raise ProductionDriverError("INVALID_MODEL_STOP_EVIDENCE", "model identity differs")
        _exact_loopback_endpoint(self.endpoint, permitted_paths=frozenset({""}))
        if (
            type(cast(object, self.leader_reaped)) is not bool
            or type(cast(object, self.port_available)) is not bool
            or type(self.session_members_remaining) is not int
            or self.session_members_remaining < 0
            or not self.leader_reaped
            or self.session_members_remaining != 0
            or not self.port_available
        ):
            raise ProductionDriverError(
                "MODEL_REAP_UNCONFIRMED", "model process/session/port release is unconfirmed"
            )


def production_model_stop_evidence_projection(
    value: ProductionModelStopEvidenceV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionModelStopEvidenceV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "model stop evidence type differs")
    return {
        "endpoint": value.endpoint,
        "host": value.host.value,
        "leader_reaped": value.leader_reaped,
        "port_available": value.port_available,
        "process": {
            "pid": value.process.pid,
            "process_group_id": value.process.process_group_id,
            "session_id": value.process.session_id,
            "starttime_ticks": value.process.starttime_ticks,
            "uid": value.process.uid,
        },
        "session_members_remaining": value.session_members_remaining,
    }


def production_model_stop_evidence_sha256(value: ProductionModelStopEvidenceV1) -> str:
    return _hash_projection(
        "production-model-stop-evidence",
        cast(JsonValue, production_model_stop_evidence_projection(value)),
    )


@dataclass(frozen=True, slots=True)
class ProductionModelHandoffEvidenceV1:
    manifest_sha256: str
    runtime_config_sha256: str
    sequence_execution_scope: str
    sequence_scope_authority_sha256: str
    resource_topology: ProductionResourceTopologyV1
    gpu_lease_sha256: str
    source_host: PilotHostV1
    target_host: PilotHostV1
    source_stop: ProductionModelStopEvidenceV1
    baseline_shared_gpu_attestation: ProductionSharedGpuAttestationV1
    post_stop_shared_gpu_attestation: ProductionSharedGpuAttestationV1
    target_command_sha256: str
    target_process: OwnedProcessIdentityV1
    target_health_sha256: str
    target_snapshot_attestation_sha256: str
    target_ready_shared_gpu_attestation: ProductionSharedGpuAttestationV1

    def __post_init__(self) -> None:
        for name in (
            "manifest_sha256",
            "runtime_config_sha256",
            "sequence_scope_authority_sha256",
            "gpu_lease_sha256",
            "target_command_sha256",
            "target_health_sha256",
            "target_snapshot_attestation_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if (
            self.resource_topology is not ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
            or self.sequence_execution_scope != "R24_LIVE_SMOKE_ONLY"
            or self.source_host is not PilotHostV1.QWEN3_VL
            or self.target_host is not PilotHostV1.MAI_UI
            or self.source_stop.host is not self.source_host
            or type(self.target_process) is not OwnedProcessIdentityV1
        ):
            raise ProductionDriverError("INVALID_MODEL_HANDOFF", "handoff topology differs")
        gpu_indices = {
            self.baseline_shared_gpu_attestation.gpu_index,
            self.post_stop_shared_gpu_attestation.gpu_index,
            self.target_ready_shared_gpu_attestation.gpu_index,
        }
        gpu_uuids = {
            self.baseline_shared_gpu_attestation.gpu_uuid,
            self.post_stop_shared_gpu_attestation.gpu_uuid,
            self.target_ready_shared_gpu_attestation.gpu_uuid,
        }
        if len(gpu_indices) != 1 or len(gpu_uuids) != 1:
            raise ProductionDriverError("INVALID_MODEL_HANDOFF", "physical GPU identity drifted")
        if (
            self.baseline_shared_gpu_attestation.minimum_free_memory_mib
            != _SHARED_MINIMUM_FREE_GPU_MEMORY_MIB_V1
            or self.post_stop_shared_gpu_attestation.minimum_free_memory_mib
            != _SHARED_MINIMUM_FREE_GPU_MEMORY_MIB_V1
            or self.target_ready_shared_gpu_attestation.minimum_free_memory_mib != 0
        ):
            raise ProductionDriverError("INVALID_MODEL_HANDOFF", "capacity thresholds differ")
        _require_shared_gpu_capacity(self.baseline_shared_gpu_attestation)
        _require_shared_gpu_capacity(self.post_stop_shared_gpu_attestation)
        _validate_shared_gpu_tenants(
            self.post_stop_shared_gpu_attestation,
            baseline=self.baseline_shared_gpu_attestation,
            owned_identity=None,
        )
        _validate_shared_gpu_tenants(
            self.target_ready_shared_gpu_attestation,
            baseline=self.baseline_shared_gpu_attestation,
            owned_identity=self.target_process,
        )


def production_model_handoff_evidence_projection(
    value: ProductionModelHandoffEvidenceV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionModelHandoffEvidenceV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "model handoff evidence type differs")
    return {
        "baseline_shared_gpu_attestation": production_shared_gpu_attestation_projection(
            value.baseline_shared_gpu_attestation
        ),
        "baseline_shared_gpu_attestation_sha256": production_shared_gpu_attestation_sha256(
            value.baseline_shared_gpu_attestation
        ),
        "gpu_lease_sha256": value.gpu_lease_sha256,
        "manifest_sha256": value.manifest_sha256,
        "post_stop_shared_gpu_attestation": production_shared_gpu_attestation_projection(
            value.post_stop_shared_gpu_attestation
        ),
        "post_stop_shared_gpu_attestation_sha256": production_shared_gpu_attestation_sha256(
            value.post_stop_shared_gpu_attestation
        ),
        "resource_topology": value.resource_topology.value,
        "runtime_config_sha256": value.runtime_config_sha256,
        "sequence_execution_scope": value.sequence_execution_scope,
        "sequence_scope_authority_sha256": value.sequence_scope_authority_sha256,
        "source_host": value.source_host.value,
        "source_stop": production_model_stop_evidence_projection(value.source_stop),
        "source_stop_sha256": production_model_stop_evidence_sha256(value.source_stop),
        "status": "COMPLETED",
        "target_command_sha256": value.target_command_sha256,
        "target_health_sha256": value.target_health_sha256,
        "target_host": value.target_host.value,
        "target_process": {
            "pid": value.target_process.pid,
            "process_group_id": value.target_process.process_group_id,
            "session_id": value.target_process.session_id,
            "starttime_ticks": value.target_process.starttime_ticks,
            "uid": value.target_process.uid,
        },
        "target_ready_shared_gpu_attestation": production_shared_gpu_attestation_projection(
            value.target_ready_shared_gpu_attestation
        ),
        "target_ready_shared_gpu_attestation_sha256": production_shared_gpu_attestation_sha256(
            value.target_ready_shared_gpu_attestation
        ),
        "target_snapshot_attestation_sha256": value.target_snapshot_attestation_sha256,
    }


def production_model_handoff_evidence_sha256(value: ProductionModelHandoffEvidenceV1) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            cast(
                JsonValue,
                {
                    "domain": "production-model-handoff-evidence",
                    "schema_version": PRODUCTION_MODEL_HANDOFF_EVIDENCE_SCHEMA_VERSION_V1,
                    "value": production_model_handoff_evidence_projection(value),
                },
            )
        )
    ).hexdigest()


def _production_model_handoff_evidence_preimage(
    value: ProductionModelHandoffEvidenceV1,
) -> bytes:
    return canonical_json_bytes(
        cast(
            JsonValue,
            {
                "domain": "production-model-handoff-evidence",
                "schema_version": PRODUCTION_MODEL_HANDOFF_EVIDENCE_SCHEMA_VERSION_V1,
                "value": production_model_handoff_evidence_projection(value),
            },
        )
    )


@dataclass(frozen=True, slots=True)
class CpuResourceLifecycleTraceV1:
    commands: tuple[tuple[str, ...], ...]
    health_endpoints: tuple[str, ...]
    cleanup_targets: tuple[str, ...]
    dispatch_attestations: tuple[str, ...]
    pending_cleanup_count: int


class _OwnedModelProcessV1:
    __slots__ = ("identity", "process", "spec", "stderr_handle", "stdout_handle")

    def __init__(
        self,
        *,
        identity: OwnedProcessIdentityV1,
        process: subprocess.Popen[bytes] | None,
        spec: ProductionCommandSpecV1,
        stdout_handle: object | None,
        stderr_handle: object | None,
    ) -> None:
        self.identity = identity
        self.process = process
        self.spec = spec
        self.stdout_handle = stdout_handle
        self.stderr_handle = stderr_handle


class _PartialModelProcessV1:
    """A Popen child claimed before complete /proc identity admission."""

    __slots__ = ("process", "spec", "stderr_handle", "stdout_handle")

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        spec: ProductionCommandSpecV1,
        stdout_handle: object,
        stderr_handle: object,
    ) -> None:
        self.process = process
        self.spec = spec
        self.stdout_handle = stdout_handle
        self.stderr_handle = stderr_handle


class _OwnedBackendContainerV1:
    __slots__ = ("container_id", "name", "spec")

    def __init__(self, *, container_id: str, name: str, spec: ProductionCommandSpecV1) -> None:
        if _SHA256.fullmatch(container_id) is None:
            raise ProductionDriverError(
                "INVALID_CONTAINER_ID", "Docker did not return one exact container ID"
            )
        self.container_id = container_id
        self.name = name
        self.spec = spec


class _ExclusiveGpuLeaseV1:
    """Process-scoped Linux abstract-socket lease for one physical GPU index.

    Abstract AF_UNIX names have kernel lifetime, so a crash releases the lease
    without leaving a lock file that a later run could mistake for authority.
    The name is per effective UID and GPU index; independent processes owned by
    the same operator cannot concurrently claim the same configured device.
    """

    __slots__ = ("gpu_index", "lease_sha256", "_socket")

    def __init__(self, gpu_index: int) -> None:
        if type(gpu_index) is not int or not 0 <= gpu_index <= 255:
            raise ProductionDriverError("INVALID_GPU_LEASE", "GPU index differs")
        if not hasattr(socket, "AF_UNIX") or not sys.platform.startswith("linux"):
            raise ProductionDriverError(
                "GPU_LEASE_UNAVAILABLE", "Linux abstract AF_UNIX leases are required"
            )
        address = f"\0mobileworld-r24-gpu-v1-uid-{os.geteuid()}-index-{gpu_index}"
        lease_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        lease_socket.set_inheritable(False)
        try:
            lease_socket.bind(address)
        except OSError as exc:
            lease_socket.close()
            raise ProductionDriverError(
                "GPU_LEASE_CONFLICT", "configured GPU already has a live owner lease"
            ) from exc
        self.gpu_index = gpu_index
        self.lease_sha256 = _hash_projection(
            "linux-abstract-gpu-lease",
            cast(JsonValue, {"gpu_index": gpu_index, "uid": os.geteuid()}),
        )
        self._socket: socket.socket | None = lease_socket

    def close(self) -> None:
        lease_socket = self._socket
        if lease_socket is not None:
            lease_socket.close()
            self._socket = None


_CPU_GPU_LEASE_LOCK: Final[threading.Lock] = threading.Lock()
_CPU_GPU_LEASES: set[int] = set()


class _CpuExclusiveGpuLeaseV1:
    """Sealed no-GPU lease double for sandboxed CPU tests."""

    __slots__ = ("gpu_index", "lease_sha256", "_held")

    def __init__(self, gpu_index: int, *, seal: object) -> None:
        if seal is not _MODULE_SEAL:
            raise PermissionError("CPU GPU lease is module-owned")
        with _CPU_GPU_LEASE_LOCK:
            if gpu_index in _CPU_GPU_LEASES:
                raise ProductionDriverError(
                    "GPU_LEASE_CONFLICT", "configured GPU already has a CPU test lease"
                )
            _CPU_GPU_LEASES.add(gpu_index)
        self.gpu_index = gpu_index
        self.lease_sha256 = _hash_projection(
            "cpu-exclusive-gpu-lease", cast(JsonValue, {"gpu_index": gpu_index})
        )
        self._held = True

    def close(self) -> None:
        if not self._held:
            return
        with _CPU_GPU_LEASE_LOCK:
            _CPU_GPU_LEASES.discard(self.gpu_index)
        self._held = False


def _read_owned_process_identity(pid: int) -> OwnedProcessIdentityV1:
    try:
        proc = Path("/proc") / str(pid)
        metadata = proc.stat()
        fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        return OwnedProcessIdentityV1(
            pid=pid,
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            starttime_ticks=int(fields[19]),
            uid=metadata.st_uid,
        )
    except (OSError, IndexError, ValueError) as exc:
        raise ProductionDriverError(
            "OWNED_PROCESS_IDENTITY_LOST", "model process identity is unavailable"
        ) from exc


def _read_shared_gpu_process_evidence(
    pid: int,
    *,
    used_gpu_memory_mib: int,
) -> SharedGpuProcessEvidenceV1:
    try:
        proc = Path("/proc") / str(pid)
        before = proc.stat()
        fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        after = proc.stat()
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_uid != after.st_uid
        ):
            raise OSError("process identity changed during shared GPU attestation")
        uid = after.st_uid
        return SharedGpuProcessEvidenceV1(
            pid=pid,
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            starttime_ticks=int(fields[19]),
            uid=uid,
            user=pwd.getpwuid(uid).pw_name,
            used_gpu_memory_mib=used_gpu_memory_mib,
        )
    except (IndexError, KeyError, OSError, ValueError) as exc:
        raise ProductionDriverError(
            "GPU_PROCESS_IDENTITY_UNAVAILABLE",
            "shared GPU compute process identity could not be proven",
        ) from exc


@dataclass(frozen=True, slots=True)
class _OwnedSessionMemberV1:
    pid: int
    process_group_id: int
    session_id: int
    starttime_ticks: int
    uid: int


def _read_owned_session_member(pid: int) -> _OwnedSessionMemberV1 | None:
    try:
        proc = Path("/proc") / str(pid)
        metadata_before = proc.stat()
        fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        metadata_after = proc.stat()
        if (
            metadata_before.st_dev != metadata_after.st_dev
            or metadata_before.st_ino != metadata_after.st_ino
            or metadata_before.st_uid != metadata_after.st_uid
        ):
            raise OSError("process identity changed while reading session membership")
        return _OwnedSessionMemberV1(
            pid=pid,
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            starttime_ticks=int(fields[19]),
            uid=metadata_after.st_uid,
        )
    except FileNotFoundError:
        return None
    except (IndexError, OSError, ValueError) as exc:
        raise ProductionDriverError(
            "MODEL_REAP_UNCONFIRMED",
            "process identity could not be inspected after model stop",
        ) from exc


def _owned_session_members(
    identity: OwnedProcessIdentityV1,
) -> tuple[_OwnedSessionMemberV1, ...]:
    members: list[_OwnedSessionMemberV1] = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise ProductionDriverError(
            "MODEL_REAP_UNCONFIRMED", "process table is unavailable after model stop"
        ) from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            member = _read_owned_session_member(int(entry.name))
        except ProductionDriverError:
            raise
        if (
            member is not None
            and member.uid == identity.uid
            and member.session_id == identity.session_id
        ):
            members.append(member)
    return tuple(sorted(members, key=lambda item: (item.pid, item.starttime_ticks)))


def _signal_owned_session_member(member: _OwnedSessionMemberV1, signum: int) -> None:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        raise ProductionDriverError(
            "MODEL_REAP_UNCONFIRMED", "pidfd process signaling is unavailable"
        )
    descriptor = -1
    try:
        descriptor = pidfd_open(member.pid, 0)
        current = _read_owned_session_member(member.pid)
        if current is None:
            return
        if current != member:
            raise ProductionDriverError(
                "MODEL_REAP_UNCONFIRMED",
                "owned session PID identity changed before signaling",
            )
        pidfd_send_signal(descriptor, signum, None, 0)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise ProductionDriverError(
            "MODEL_REAP_UNCONFIRMED", "owned session member could not be signaled"
        ) from exc
    except OSError as exc:
        raise ProductionDriverError(
            "MODEL_REAP_UNCONFIRMED", "owned session pidfd signaling failed"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _drain_owned_session(
    identity: OwnedProcessIdentityV1,
    *,
    shutdown_grace_seconds: int,
    poll_interval_ms: int,
) -> tuple[_OwnedSessionMemberV1, ...]:
    """Signal only stable PID identities proven to belong to one owned session."""

    members = _owned_session_members(identity)
    for member in members:
        _signal_owned_session_member(member, signal.SIGTERM)
    term_deadline_ns = time.monotonic_ns() + shutdown_grace_seconds * 1_000_000_000
    while members and time.monotonic_ns() < term_deadline_ns:
        time.sleep(poll_interval_ms / 1_000)
        members = _owned_session_members(identity)
    kill_deadline_ns = time.monotonic_ns() + shutdown_grace_seconds * 1_000_000_000
    while members and time.monotonic_ns() < kill_deadline_ns:
        for member in members:
            _signal_owned_session_member(member, signal.SIGKILL)
        time.sleep(poll_interval_ms / 1_000)
        members = _owned_session_members(identity)
    return members


def _regular_file_sha256(
    path_text: str,
    *,
    expected_byte_count: int,
    require_executable: bool,
) -> str:
    path = Path(path_text)
    descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise ProductionDriverError(
                "NOFOLLOW_UNAVAILABLE", "platform cannot attest resource files"
            )
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow)
        opened = os.fstat(descriptor)
        declared = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != declared.st_dev
            or opened.st_ino != declared.st_ino
            or opened.st_size != expected_byte_count
            or (require_executable and opened.st_mode & 0o111 == 0)
        ):
            raise ProductionDriverError(
                "RESOURCE_FILE_BINDING_MISMATCH", "resource file metadata differs"
            )
        digest = hashlib.sha256()
        remaining = expected_byte_count
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                raise ProductionDriverError(
                    "RESOURCE_FILE_BINDING_MISMATCH", "resource file truncated during attestation"
                )
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ProductionDriverError(
                "RESOURCE_FILE_BINDING_MISMATCH", "resource file grew during attestation"
            )
        return digest.hexdigest()
    except OSError as exc:
        raise ProductionDriverError(
            "RESOURCE_FILE_BINDING_MISMATCH", "resource file is unavailable"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _attest_vllm_executable(config: ProductionRuntimeConfigV1) -> str:
    declared = Path(config.vllm_python_executable)
    try:
        declared.lstat()
        resolved = declared.resolve(strict=True)
    except OSError as exc:
        raise ProductionDriverError(
            "VLLM_EXECUTABLE_BINDING_MISMATCH",
            "declared vLLM Python executable is unavailable",
        ) from exc
    if str(resolved) != config.vllm_python_realpath:
        raise ProductionDriverError(
            "VLLM_EXECUTABLE_BINDING_MISMATCH",
            "vLLM Python executable realpath differs",
        )
    digest = _regular_file_sha256(
        str(resolved),
        expected_byte_count=config.vllm_python_byte_count,
        require_executable=True,
    )
    if digest != config.vllm_python_sha256:
        raise ProductionDriverError(
            "VLLM_EXECUTABLE_BINDING_MISMATCH",
            "vLLM Python executable content differs",
        )
    return digest


def _attest_openai_sdk_version(stages: tuple[OpenAIResponsesStageV1, ...]) -> str:
    if type(stages) is not tuple or not stages:
        raise ProductionDriverError(
            "OPENAI_SDK_VERSION_MISMATCH", "OpenAI stage authority is absent"
        )
    expected = {stage.openai_sdk_version for stage in stages}
    try:
        installed = importlib.metadata.version("openai")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ProductionDriverError(
            "OPENAI_SDK_VERSION_MISMATCH", "OpenAI SDK distribution is unavailable"
        ) from exc
    if len(expected) != 1 or installed not in expected:
        raise ProductionDriverError(
            "OPENAI_SDK_VERSION_MISMATCH",
            "installed OpenAI SDK version differs from manifest authority",
        )
    return installed


def _attest_snapshot_resource(resource: SnapshotResourceV1) -> str:
    try:
        digest = compute_snapshot_tree_digest(resource)
    except Exception as exc:
        raise ProductionDriverError(
            "SNAPSHOT_TREE_BINDING_MISMATCH", "model snapshot cannot be re-attested"
        ) from exc
    if (
        digest.sha256 != resource.snapshot_tree_sha256
        or digest.total_bytes != resource.snapshot_total_bytes
        or digest.file_count != resource.snapshot_file_count
    ):
        raise ProductionDriverError(
            "SNAPSHOT_TREE_BINDING_MISMATCH",
            "model snapshot tree differs from manifest authority",
        )
    return digest.sha256


def _attest_backend_environment_file(config: ProductionRuntimeConfigV1) -> str:
    path = Path(config.backend_environment_file)
    repository = Path(config.repository_root)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        repository_resolved = repository.resolve(strict=True)
    except OSError as exc:
        raise ProductionDriverError(
            "BACKEND_ENVIRONMENT_BINDING_MISMATCH",
            "backend environment file is unavailable",
        ) from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or resolved.is_relative_to(repository_resolved)
        or metadata.st_dev != config.backend_environment_file_device
        or metadata.st_ino != config.backend_environment_file_inode
        or stat.S_IMODE(metadata.st_mode) != config.backend_environment_file_mode
        or metadata.st_uid != config.backend_environment_file_uid
        or metadata.st_size != config.backend_environment_file_byte_count
        or metadata.st_mtime_ns != config.backend_environment_file_mtime_ns
    ):
        raise ProductionDriverError(
            "BACKEND_ENVIRONMENT_BINDING_MISMATCH",
            "backend environment file must be owner-only and repository-external",
        )
    # Never read, hash, log, or copy env-file contents: a content digest of a
    # low-entropy credential is itself sensitive.  Re-open with O_NOFOLLOW and
    # bind only stable metadata immediately before Docker receives the path.
    descriptor = -1
    try:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise ProductionDriverError(
                "NOFOLLOW_UNAVAILABLE", "platform cannot attest resource files"
            )
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_mode != metadata.st_mode
            or opened.st_uid != metadata.st_uid
            or opened.st_size != metadata.st_size
            or opened.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise ProductionDriverError(
                "BACKEND_ENVIRONMENT_BINDING_MISMATCH",
                "backend environment metadata changed during attestation",
            )
    except OSError as exc:
        raise ProductionDriverError(
            "BACKEND_ENVIRONMENT_BINDING_MISMATCH",
            "backend environment file is unavailable",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _hash_projection(
        "backend-environment-file-metadata",
        cast(
            JsonValue,
            {
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "mode": stat.S_IMODE(metadata.st_mode),
                "mtime_ns": metadata.st_mtime_ns,
                "path": str(resolved),
                "size": metadata.st_size,
                "uid": metadata.st_uid,
            },
        ),
    )


def _runtime_attestation_sha256(
    config: ProductionRuntimeConfigV1,
    *,
    source_commit: str,
    source_tree_git_sha1: str,
) -> str:
    return _hash_projection(
        "production-runtime-attestation",
        cast(
            JsonValue,
            {
                "backend_environment_file_metadata_sha256": _hash_projection(
                    "backend-environment-file-metadata",
                    cast(
                        JsonValue,
                        {
                            "device": config.backend_environment_file_device,
                            "inode": config.backend_environment_file_inode,
                            "mode": config.backend_environment_file_mode,
                            "mtime_ns": config.backend_environment_file_mtime_ns,
                            "path": config.backend_environment_file,
                            "size": config.backend_environment_file_byte_count,
                            "uid": config.backend_environment_file_uid,
                        },
                    ),
                ),
                "backend_image_id": f"sha256:{config.backend_image_id_sha256}",
                "backend_network": _DOCKER_NETWORK,
                "mobileworld_source_root": config.mobileworld_source_root,
                "mobileworld_source_tree_git_sha1": source_tree_git_sha1,
                "runtime_config_sha256": production_runtime_config_sha256(config),
                "source_commit": source_commit,
                "vllm_python_sha256": config.vllm_python_sha256,
                "vllm_version": config.vllm_version,
            },
        ),
    )


def _assert_loopback_port_free(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise ProductionDriverError(
            "LOOPBACK_PORT_BUSY", "an authorized loopback port is already occupied"
        ) from exc
    finally:
        probe.close()


def _bounded_http_bytes(endpoint: str, path: str, *, timeout_seconds: float) -> bytes:
    host, port, _ = _exact_loopback_endpoint(endpoint, permitted_paths=frozenset({""}))
    connection = http.client.HTTPConnection(host, port, timeout=timeout_seconds)
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(_MAX_HEALTH_RESPONSE_BYTES + 1)
        if response.status != 200 or len(raw) > _MAX_HEALTH_RESPONSE_BYTES:
            raise ProductionDriverError(
                "RESOURCE_HEALTH_FAILED", "loopback health response is not bounded HTTP 200"
            )
        return raw
    except (OSError, http.client.HTTPException) as exc:
        raise ProductionDriverError(
            "RESOURCE_HEALTH_FAILED", "loopback health response is unavailable"
        ) from exc
    finally:
        connection.close()


def _bounded_http_json(endpoint: str, path: str, *, timeout_seconds: float) -> object:
    raw = _bounded_http_bytes(endpoint, path, timeout_seconds=timeout_seconds)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProductionDriverError(
            "RESOURCE_HEALTH_FAILED", "loopback health response is not JSON"
        ) from exc


class _PosixProductionResourceSystemV1:
    """Concrete production I/O owner.  All commands originate in this module."""

    __slots__ = (
        "_backend_candidates",
        "_config",
        "_models",
        "_pending_backend_names",
        "_partial_models",
        "_pending_backend_ids",
    )

    def __init__(self, config: ProductionRuntimeConfigV1, *, seal: object) -> None:
        if seal is not _MODULE_SEAL or type(config) is not ProductionRuntimeConfigV1:
            raise PermissionError("production resource system is module-owned")
        if os.name != "posix":
            raise ProductionDriverError(
                "PROCESS_CONTROL_UNAVAILABLE", "POSIX process ownership is required"
            )
        self._config = config
        self._models: dict[int, _OwnedModelProcessV1] = {}
        self._partial_models: dict[int, _PartialModelProcessV1] = {}
        self._backend_candidates: dict[str, _OwnedBackendContainerV1] = {}
        self._pending_backend_ids: set[str] = set()
        self._pending_backend_names: dict[str, ProductionCommandSpecV1] = {}

    @staticmethod
    def _docker_run(
        argv: tuple[str, ...], *, timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        if not argv or argv[0] != _DOCKER_EXECUTABLE:
            raise ProductionDriverError("INVALID_DOCKER_COMMAND", "Docker argv differs")
        try:
            return subprocess.run(
                list(argv),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env={"PATH": "/usr/bin:/bin"},
            )
        except subprocess.TimeoutExpired as exc:
            raise ProductionDriverError(
                "DOCKER_COMMAND_TIMEOUT", "fixed Docker command exceeded its bound"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProductionDriverError(
                "DOCKER_COMMAND_FAILED", "fixed Docker command did not complete"
            ) from exc

    @staticmethod
    def _attestation_run(
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(argv),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=_RESOURCE_ATTESTATION_COMMAND_TIMEOUT_SECONDS_V1,
                cwd=cwd,
                env={
                    "DO_NOT_TRACK": "1",
                    "HF_HUB_DISABLE_TELEMETRY": "1",
                    "HF_HUB_OFFLINE": "1",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONSAFEPATH": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "VLLM_NO_USAGE_STATS": "1",
                },
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProductionDriverError(
                "RUNTIME_ATTESTATION_FAILED",
                "fixed runtime-attestation command did not complete",
            ) from exc

    def attest_runtime(self, config: ProductionRuntimeConfigV1, *, source_commit: str) -> str:
        if config != self._config or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
            raise ProductionDriverError(
                "RUNTIME_ATTESTATION_FAILED", "runtime attestation authority differs"
            )
        _attest_vllm_executable(config)
        _attest_backend_environment_file(config)
        repository = Path(config.repository_root).resolve(strict=True)
        source = Path(config.mobileworld_source_root)
        try:
            source_metadata = source.lstat()
            source_resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ProductionDriverError(
                "SOURCE_TREE_BINDING_MISMATCH", "MobileWorld source tree is unavailable"
            ) from exc
        if (
            source.is_symlink()
            or not stat.S_ISDIR(source_metadata.st_mode)
            or source_resolved != repository / "MobileWorld" / "src"
        ):
            raise ProductionDriverError(
                "SOURCE_TREE_BINDING_MISMATCH", "MobileWorld source mount differs"
            )

        git_prefix = (
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
        )
        head = self._attestation_run((*git_prefix, "rev-parse", "HEAD"), cwd=repository)
        status = self._attestation_run(
            (*git_prefix, "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=repository,
        )
        tree = self._attestation_run(
            (*git_prefix, "rev-parse", f"{source_commit}:MobileWorld/src"),
            cwd=repository,
        )
        tree_sha1 = tree.stdout.strip()
        if (
            head.returncode != 0
            or head.stderr
            or head.stdout.strip() != source_commit
            or status.returncode != 0
            or status.stderr
            or status.stdout
            or tree.returncode != 0
            or tree.stderr
            or re.fullmatch(r"[0-9a-f]{40}", tree_sha1) is None
        ):
            raise ProductionDriverError(
                "SOURCE_TREE_BINDING_MISMATCH",
                "source tree is not the clean authorized commit",
            )

        version_argv = (
            config.vllm_python_executable,
            "-P",
            "-B",
            "-c",
            "import importlib.metadata as m; print(m.version('vllm'))",
        )
        version = self._attestation_run(version_argv)
        if (
            version.returncode != 0
            or version.stderr
            or version.stdout.strip() != config.vllm_version
        ):
            raise ProductionDriverError(
                "VLLM_VERSION_BINDING_MISMATCH", "installed vLLM version differs"
            )
        network = self._docker_run(
            (_DOCKER_EXECUTABLE, "network", "inspect", "--format", "{{.Name}}", _DOCKER_NETWORK),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        image = self._docker_run(
            (
                _DOCKER_EXECUTABLE,
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                _MOBILEWORLD_BACKEND_IMAGE,
            ),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if (
            network.returncode != 0
            or network.stderr
            or network.stdout.strip() != _DOCKER_NETWORK
            or image.returncode != 0
            or image.stderr
            or image.stdout.strip() != f"sha256:{config.backend_image_id_sha256}"
        ):
            raise ProductionDriverError(
                "DOCKER_RESOURCE_BINDING_MISMATCH", "Docker network or image differs"
            )
        return _runtime_attestation_sha256(
            config,
            source_commit=source_commit,
            source_tree_git_sha1=tree_sha1,
        )

    def attest_gpu_idle(self, gpu_index: int) -> str:
        identity_command = (
            _NVIDIA_SMI_EXECUTABLE,
            f"--id={gpu_index}",
            "--query-gpu=index,uuid",
            "--format=csv,noheader,nounits",
        )
        identity = self._attestation_run(identity_command)
        identity_fields = tuple(item.strip() for item in identity.stdout.strip().split(","))
        if (
            identity.returncode != 0
            or identity.stderr
            or len(identity_fields) != 2
            or identity_fields[0] != str(gpu_index)
            or _NVIDIA_GPU_UUID.fullmatch(identity_fields[1]) is None
        ):
            raise ProductionDriverError(
                "GPU_IDENTITY_MISMATCH",
                "configured GPU index does not resolve to one exact physical GPU UUID",
            )
        process_command = (
            _NVIDIA_SMI_EXECUTABLE,
            f"--id={gpu_index}",
            "--query-compute-apps=gpu_uuid,pid",
            "--format=csv,noheader,nounits",
        )
        processes = self._attestation_run(process_command)
        if processes.returncode != 0 or processes.stderr or processes.stdout.strip():
            raise ProductionDriverError(
                "GPU_ALREADY_OCCUPIED",
                "configured GPU has a compute process or cannot be attested idle",
            )
        return _hash_projection(
            "production-gpu-idle-attestation",
            cast(
                JsonValue,
                {
                    "gpu_index": gpu_index,
                    "gpu_uuid": identity_fields[1].lower(),
                    "identity_query": list(identity_command),
                    "process_query": list(process_command),
                },
            ),
        )

    def attest_gpu_shared_capacity(
        self,
        gpu_index: int,
        *,
        minimum_free_memory_mib: int,
    ) -> ProductionSharedGpuAttestationV1:
        identity_command = (
            _NVIDIA_SMI_EXECUTABLE,
            f"--id={gpu_index}",
            "--query-gpu=index,uuid,memory.total,memory.free,memory.used,memory.reserved,"
            "utilization.gpu,utilization.memory",
            "--format=csv,noheader,nounits",
        )
        identity = self._attestation_run(identity_command)
        identity_fields = tuple(item.strip() for item in identity.stdout.strip().split(","))
        if (
            identity.returncode != 0
            or identity.stderr
            or len(identity_fields) != 8
            or identity_fields[0] != str(gpu_index)
            or _NVIDIA_GPU_UUID.fullmatch(identity_fields[1]) is None
        ):
            raise ProductionDriverError(
                "GPU_IDENTITY_MISMATCH",
                "configured shared GPU does not resolve to one physical GPU census",
            )
        try:
            total_memory_mib = int(identity_fields[2])
            free_memory_mib = int(identity_fields[3])
            used_memory_mib = int(identity_fields[4])
            reserved_memory_mib = int(identity_fields[5])
            gpu_utilization_percent = int(identity_fields[6])
            memory_utilization_percent = int(identity_fields[7])
        except ValueError as exc:
            raise ProductionDriverError(
                "GPU_CAPACITY_ATTESTATION_FAILED", "shared GPU capacity fields differ"
            ) from exc
        process_command = (
            _NVIDIA_SMI_EXECUTABLE,
            f"--id={gpu_index}",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        )
        process_result = self._attestation_run(process_command)
        if process_result.returncode != 0 or process_result.stderr:
            raise ProductionDriverError(
                "GPU_PROCESS_ATTESTATION_FAILED", "shared GPU compute census is unavailable"
            )
        processes: list[SharedGpuProcessEvidenceV1] = []
        for line in process_result.stdout.splitlines():
            if not line.strip():
                continue
            fields = tuple(item.strip() for item in line.split(","))
            if len(fields) != 3 or fields[0].lower() != identity_fields[1].lower():
                raise ProductionDriverError(
                    "GPU_PROCESS_ATTESTATION_FAILED", "shared GPU process row differs"
                )
            try:
                pid = int(fields[1])
                process_memory_mib = int(fields[2])
            except ValueError as exc:
                raise ProductionDriverError(
                    "GPU_PROCESS_ATTESTATION_FAILED", "shared GPU process memory differs"
                ) from exc
            processes.append(
                _read_shared_gpu_process_evidence(
                    pid,
                    used_gpu_memory_mib=process_memory_mib,
                )
            )
        process_result_after = self._attestation_run(process_command)
        if (
            process_result_after.returncode != 0
            or process_result_after.stderr
            or process_result_after.stdout != process_result.stdout
        ):
            raise ProductionDriverError(
                "GPU_PROCESS_ATTESTATION_RACED",
                "shared GPU compute rows changed during process identity binding",
            )
        identity_after = self._attestation_run(identity_command)
        identity_fields_after = tuple(
            item.strip() for item in identity_after.stdout.strip().split(",")
        )
        if (
            identity_after.returncode != 0
            or identity_after.stderr
            or len(identity_fields_after) != 8
            or identity_fields_after[0] != identity_fields[0]
            or identity_fields_after[1].lower() != identity_fields[1].lower()
            or identity_fields_after[2] != identity_fields[2]
        ):
            raise ProductionDriverError(
                "GPU_CAPACITY_ATTESTATION_RACED",
                "shared GPU identity or total memory changed during process binding",
            )
        try:
            total_memory_mib = int(identity_fields_after[2])
            free_memory_mib = int(identity_fields_after[3])
            used_memory_mib = int(identity_fields_after[4])
            reserved_memory_mib = int(identity_fields_after[5])
            gpu_utilization_percent = int(identity_fields_after[6])
            memory_utilization_percent = int(identity_fields_after[7])
        except ValueError as exc:
            raise ProductionDriverError(
                "GPU_CAPACITY_ATTESTATION_FAILED", "shared GPU capacity fields differ"
            ) from exc
        return ProductionSharedGpuAttestationV1(
            gpu_index=gpu_index,
            gpu_uuid=identity_fields[1],
            total_memory_mib=total_memory_mib,
            free_memory_mib=free_memory_mib,
            used_memory_mib=used_memory_mib,
            reserved_memory_mib=reserved_memory_mib,
            gpu_utilization_percent=gpu_utilization_percent,
            memory_utilization_percent=memory_utilization_percent,
            minimum_free_memory_mib=minimum_free_memory_mib,
            processes=tuple(sorted(processes, key=lambda item: (item.pid, item.starttime_ticks))),
        )

    def start_model(
        self,
        spec: ProductionCommandSpecV1,
        *,
        log_label: str,
    ) -> _OwnedModelProcessV1:
        if (
            spec.kind not in {"VLLM_QWEN", "VLLM_MAI"}
            or spec.argv[0] != self._config.vllm_python_executable
        ):
            raise ProductionDriverError("INVALID_MODEL_COMMAND", "model argv differs")
        _attest_vllm_executable(self._config)
        _, port, _ = _exact_loopback_endpoint(spec.endpoint, permitted_paths=frozenset({""}))
        _assert_loopback_port_free(port)
        log_root = Path(self._config.process_log_root)
        if not log_root.exists():
            log_root.mkdir(mode=0o700, parents=False)
            log_root.chmod(0o700)
        try:
            log_metadata = log_root.lstat()
        except OSError as exc:
            raise ProductionDriverError("INVALID_LOG_ROOT", "process log root is absent") from exc
        if (
            log_root.is_symlink()
            or not stat.S_ISDIR(log_metadata.st_mode)
            or stat.S_IMODE(log_metadata.st_mode) != 0o700
            or log_metadata.st_uid != os.geteuid()
        ):
            raise ProductionDriverError(
                "INVALID_LOG_ROOT", "process log root must be owner-only 0700"
            )
        stdout_path = log_root / f"{log_label}.stdout.log"
        stderr_path = log_root / f"{log_label}.stderr.log"
        if any(path.exists() or path.is_symlink() for path in (stdout_path, stderr_path)):
            raise ProductionDriverError("LOG_PATH_EXISTS", "model log target is not fresh")
        log_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        log_flags |= getattr(os, "O_NOFOLLOW", 0)
        stdout_descriptor = os.open(stdout_path, log_flags, 0o600)
        try:
            stderr_descriptor = os.open(stderr_path, log_flags, 0o600)
        except Exception:
            os.close(stdout_descriptor)
            stdout_path.unlink(missing_ok=True)
            raise
        try:
            os.fchmod(stdout_descriptor, 0o600)
            os.fchmod(stderr_descriptor, 0o600)
        except Exception:
            os.close(stdout_descriptor)
            os.close(stderr_descriptor)
            stdout_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)
            raise
        stdout_handle = os.fdopen(stdout_descriptor, "wb", buffering=0)
        stderr_handle = os.fdopen(stderr_descriptor, "wb", buffering=0)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                list(spec.argv),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=dict(spec.environment),
                close_fds=True,
                start_new_session=True,
            )
            self._partial_models[process.pid] = _PartialModelProcessV1(
                process,
                spec=spec,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
            )
            identity = _read_owned_process_identity(process.pid)
            owned = _OwnedModelProcessV1(
                identity=identity,
                process=process,
                spec=spec,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
            )
            self._models[identity.pid] = owned
            self._partial_models.pop(identity.pid, None)
            return owned
        except Exception as exc:
            if process is None:
                stdout_handle.close()
                stderr_handle.close()
                raise
            try:
                self._stop_partial_model(process.pid)
            except Exception as cleanup_exc:
                raise ProductionDriverError(
                    "MODEL_PARTIAL_START_CLEANUP_FAILED",
                    "partially started model remains owned for cleanup retry",
                ) from cleanup_exc
            raise ProductionDriverError(
                "MODEL_PARTIAL_START_RECOVERED",
                "partially started model was reclaimed before admission",
            ) from exc

    def _stop_partial_model(self, pid: int) -> None:
        partial = self._partial_models.get(pid)
        if partial is None:
            return
        process = partial.process
        partial_identity = OwnedProcessIdentityV1(
            pid=pid,
            process_group_id=pid,
            session_id=pid,
            starttime_ticks=0,
            uid=os.geteuid(),
        )
        if process.poll() is None:
            try:
                process_group_id = os.getpgid(pid)
                session_id = os.getsid(pid)
            except OSError as exc:
                raise ProductionDriverError(
                    "MODEL_PARTIAL_IDENTITY_LOST",
                    "partial model process identity is unavailable",
                ) from exc
            if process_group_id != pid or session_id != pid:
                raise ProductionDriverError(
                    "MODEL_PARTIAL_IDENTITY_LOST",
                    "partial model did not retain its owned session",
                )
            os.killpg(process_group_id, signal.SIGKILL)
            try:
                process.wait(timeout=self._config.shutdown_grace_seconds)
            except subprocess.TimeoutExpired as exc:
                raise ProductionDriverError(
                    "MODEL_PARTIAL_CLEANUP_FAILED", "partial model did not exit"
                ) from exc
        session_members = _drain_owned_session(
            partial_identity,
            shutdown_grace_seconds=self._config.shutdown_grace_seconds,
            poll_interval_ms=self._config.health_poll_interval_ms,
        )
        _, port, _ = _exact_loopback_endpoint(
            partial.spec.endpoint, permitted_paths=frozenset({""})
        )
        try:
            _assert_loopback_port_free(port)
        except ProductionDriverError as exc:
            raise ProductionDriverError(
                "MODEL_PARTIAL_CLEANUP_FAILED",
                "partial model loopback port remains live",
            ) from exc
        if session_members:
            raise ProductionDriverError(
                "MODEL_PARTIAL_CLEANUP_FAILED",
                "partial model session remains live",
            )
        for handle in (partial.stdout_handle, partial.stderr_handle):
            close = getattr(handle, "close", None)
            if callable(close):
                close()
        self._partial_models.pop(pid, None)

    @staticmethod
    def _health_once(spec: ProductionCommandSpecV1) -> str:
        health = _bounded_http_bytes(spec.endpoint, "/health", timeout_seconds=2.0)
        models = _bounded_http_json(spec.endpoint, "/v1/models", timeout_seconds=2.0)
        if type(models) is not dict:
            raise ProductionDriverError("RESOURCE_HEALTH_FAILED", "model registry is not an object")
        raw_data = cast(dict[object, object], models).get("data")
        if (
            type(raw_data) is not list
            or len(raw_data) != 1
            or type(raw_data[0]) is not dict
            or raw_data[0].get("id") != spec.expected_model_id
        ):
            raise ProductionDriverError(
                "RESOURCE_HEALTH_FAILED", "served model ID is absent from loopback registry"
            )
        return _hash_projection(
            "model-health",
            cast(
                JsonValue,
                {
                    "health_byte_count": len(health),
                    "health_sha256": hashlib.sha256(health).hexdigest(),
                    "models": models,
                    "spec": production_command_spec_sha256(spec),
                },
            ),
        )

    def await_model(self, owned: _OwnedModelProcessV1, *, deadline_ns: int) -> str:
        last_error: Exception | None = None
        while time.monotonic_ns() < deadline_ns:
            process = owned.process
            if process is None or process.poll() is not None:
                raise ProductionDriverError(
                    "MODEL_PROCESS_EXITED", "model server exited before readiness"
                )
            try:
                return self._health_once(owned.spec)
            except Exception as exc:
                last_error = exc
            time.sleep(self._config.health_poll_interval_ms / 1_000)
        raise ProductionDriverError(
            "MODEL_STARTUP_TIMEOUT", "model server did not become ready"
        ) from last_error

    def attest_model(self, owned: _OwnedModelProcessV1) -> str:
        registered = self._models.get(owned.identity.pid)
        process = owned.process
        if registered is not owned or process is None or process.poll() is not None:
            raise ProductionDriverError(
                "MODEL_DISPATCH_IDENTITY_LOST", "model process is not the live owned child"
            )
        if _read_owned_process_identity(owned.identity.pid) != owned.identity:
            raise ProductionDriverError(
                "MODEL_DISPATCH_IDENTITY_LOST", "model PID/starttime identity changed"
            )
        return self._health_once(owned.spec)

    def stop_model(self, owned: _OwnedModelProcessV1) -> ProductionModelStopEvidenceV1:
        registered = self._models.get(owned.identity.pid)
        if registered is not owned:
            raise ProductionDriverError("UNOWNED_PROCESS", "model process is not module-owned")
        process = owned.process
        if process is not None and process.poll() is None:
            current = _read_owned_process_identity(owned.identity.pid)
            if current != owned.identity:
                raise ProductionDriverError(
                    "OWNED_PROCESS_IDENTITY_LOST", "PID identity changed before cleanup"
                )
            os.killpg(owned.identity.process_group_id, signal.SIGTERM)
            try:
                process.wait(timeout=self._config.shutdown_grace_seconds)
            except subprocess.TimeoutExpired:
                current = _read_owned_process_identity(owned.identity.pid)
                if current != owned.identity:
                    raise ProductionDriverError(
                        "OWNED_PROCESS_IDENTITY_LOST", "PID changed during cleanup"
                    )
                os.killpg(owned.identity.process_group_id, signal.SIGKILL)
                process.wait(timeout=self._config.shutdown_grace_seconds)
        session_members = _drain_owned_session(
            owned.identity,
            shutdown_grace_seconds=self._config.shutdown_grace_seconds,
            poll_interval_ms=self._config.health_poll_interval_ms,
        )
        port_available = False
        _, port, _ = _exact_loopback_endpoint(owned.spec.endpoint, permitted_paths=frozenset({""}))
        port_deadline_ns = time.monotonic_ns() + self._config.shutdown_grace_seconds * 1_000_000_000
        while time.monotonic_ns() < port_deadline_ns:
            try:
                _assert_loopback_port_free(port)
            except ProductionDriverError:
                port_available = False
            else:
                port_available = True
            if port_available:
                break
            time.sleep(self._config.health_poll_interval_ms / 1_000)
        if session_members or not port_available:
            raise ProductionDriverError(
                "MODEL_REAP_UNCONFIRMED",
                "owned model session or loopback port remains live after shutdown",
            )
        for handle in (owned.stdout_handle, owned.stderr_handle):
            close = getattr(handle, "close", None)
            if callable(close):
                close()
        self._models.pop(owned.identity.pid, None)
        return ProductionModelStopEvidenceV1(
            host=(PilotHostV1.QWEN3_VL if owned.spec.kind == "VLLM_QWEN" else PilotHostV1.MAI_UI),
            process=owned.identity,
            endpoint=owned.spec.endpoint,
            leader_reaped=True,
            session_members_remaining=0,
            port_available=True,
        )

    def start_backend(self, spec: ProductionCommandSpecV1) -> _OwnedBackendContainerV1:
        if spec.kind != "MOBILEWORLD_BACKEND":
            raise ProductionDriverError("INVALID_BACKEND_COMMAND", "backend spec differs")
        _attest_backend_environment_file(self._config)
        _, port, _ = _exact_loopback_endpoint(spec.endpoint, permitted_paths=frozenset({""}))
        _assert_loopback_port_free(port)
        name = spec.argv[spec.argv.index("--name") + 1]
        existing = self._docker_run(
            (_DOCKER_EXECUTABLE, "container", "inspect", name),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if existing.returncode == 0:
            raise ProductionDriverError(
                "BACKEND_CONTAINER_EXISTS", "driver will not reuse or remove an existing container"
            )
        if not self._docker_inspect_confirms_absent(existing, name):
            raise ProductionDriverError(
                "BACKEND_PREFLIGHT_INSPECT_FAILED",
                "fixed backend name absence cannot be established",
            )
        self._pending_backend_names[name] = spec
        try:
            launched = self._docker_run(
                spec.argv, timeout_seconds=self._config.startup_timeout_seconds
            )
        except ProductionDriverError as exc:
            candidate = self._recover_backend_candidate(name, spec)
            if candidate is None:
                if exc.code != "DOCKER_COMMAND_TIMEOUT":
                    self._pending_backend_names.pop(name, None)
                raise ProductionDriverError(
                    (
                        "BACKEND_PARTIAL_START_UNRESOLVED"
                        if exc.code == "DOCKER_COMMAND_TIMEOUT"
                        else "BACKEND_START_FAILED"
                    ),
                    "Docker launch failed before an owned ID was admitted",
                ) from exc
            try:
                self._stop_backend_capability(candidate)
            except Exception as cleanup_exc:
                raise ProductionDriverError(
                    "BACKEND_PARTIAL_START_CLEANUP_FAILED",
                    "timed-out backend remains owned for cleanup retry",
                ) from cleanup_exc
            raise ProductionDriverError(
                "BACKEND_PARTIAL_START_RECOVERED",
                "timed-out backend was reconciled and reclaimed",
            ) from exc
        container_id = launched.stdout.strip()
        if launched.returncode != 0 or _SHA256.fullmatch(container_id) is None:
            candidate = self._recover_backend_candidate(name, spec)
            if candidate is not None:
                try:
                    self._stop_backend_capability(candidate)
                except Exception as cleanup_exc:
                    raise ProductionDriverError(
                        "BACKEND_PARTIAL_START_CLEANUP_FAILED",
                        "failed backend remains owned for cleanup retry",
                    ) from cleanup_exc
                raise ProductionDriverError(
                    "BACKEND_PARTIAL_START_RECOVERED",
                    "failed backend was reconciled and reclaimed",
                )
            self._pending_backend_names.pop(name, None)
            raise ProductionDriverError("BACKEND_START_FAILED", "Docker launch failed")
        owned = _OwnedBackendContainerV1(container_id=container_id, name=name, spec=spec)
        self._backend_candidates[container_id] = owned
        self._pending_backend_names.pop(name, None)
        inspected = self._docker_run(
            (
                _DOCKER_EXECUTABLE,
                "container",
                "inspect",
                "--format",
                "{{.Id}} {{.Name}} {{.Image}} {{.State.Running}}",
                container_id,
            ),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if inspected.returncode != 0 or inspected.stdout.strip() != (
            f"{container_id} /{name} sha256:{self._config.backend_image_id_sha256} true"
        ):
            self._pending_backend_ids.add(container_id)
            try:
                self._stop_backend_capability(owned)
            except Exception as cleanup_exc:
                raise ProductionDriverError(
                    "BACKEND_OWNERSHIP_CLEANUP_FAILED",
                    "mismatched backend remains owned by its returned ID",
                ) from cleanup_exc
            raise ProductionDriverError(
                "BACKEND_OWNERSHIP_MISMATCH_RECOVERED",
                "mismatched launched container was reclaimed",
            )
        return owned

    def _recover_backend_candidate(
        self, name: str, spec: ProductionCommandSpecV1
    ) -> _OwnedBackendContainerV1 | None:
        inspected = self._docker_run(
            (
                _DOCKER_EXECUTABLE,
                "container",
                "inspect",
                "--format",
                "{{.Id}} {{.Name}} {{.Image}}",
                name,
            ),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if inspected.returncode != 0:
            if self._docker_inspect_confirms_absent(inspected, name):
                return None
            raise ProductionDriverError(
                "BACKEND_RECOVERY_INSPECT_FAILED",
                "post-launch backend absence cannot be established",
            )
        fields = inspected.stdout.strip().split()
        if (
            len(fields) != 3
            or _SHA256.fullmatch(fields[0]) is None
            or fields[1] != f"/{name}"
            or fields[2] != f"sha256:{self._config.backend_image_id_sha256}"
        ):
            raise ProductionDriverError(
                "BACKEND_RECOVERY_OWNERSHIP_MISMATCH",
                "post-launch backend cannot be attributed to the fixed command",
            )
        owned = _OwnedBackendContainerV1(container_id=fields[0], name=name, spec=spec)
        self._backend_candidates[owned.container_id] = owned
        self._pending_backend_ids.add(owned.container_id)
        return owned

    @staticmethod
    def _docker_inspect_confirms_absent(
        result: subprocess.CompletedProcess[str], target: str
    ) -> bool:
        return (
            result.returncode == 1
            # Docker CLI emits either no stdout or the canonical empty JSON
            # array for a missing inspect target, depending on CLI release.
            and result.stdout in {"", "[]", "[]\n"}
            and result.stderr.strip()
            in {
                f"Error: No such object: {target}",
                f"Error: No such container: {target}",
            }
        )

    def _backend_capability_is_gone(self, owned: _OwnedBackendContainerV1) -> bool:
        by_id = self._docker_run(
            (_DOCKER_EXECUTABLE, "container", "inspect", owned.container_id),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if not self._docker_inspect_confirms_absent(by_id, owned.container_id):
            return False
        by_name = self._docker_run(
            (_DOCKER_EXECUTABLE, "container", "inspect", owned.name),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if self._docker_inspect_confirms_absent(by_name, owned.name):
            return True
        if by_name.returncode == 0:
            # A different container may now own the fixed name.  Never stop it.
            return False
        raise ProductionDriverError(
            "BACKEND_CLEANUP_INSPECT_FAILED",
            "backend absence cannot be established by both ID and fixed name",
        )

    def _forget_backend_capability(self, owned: _OwnedBackendContainerV1) -> None:
        self._backend_candidates.pop(owned.container_id, None)
        self._pending_backend_ids.discard(owned.container_id)
        self._pending_backend_names.pop(owned.name, None)

    def _stop_backend_capability(self, owned: _OwnedBackendContainerV1) -> None:
        if self._backend_candidates.get(owned.container_id) is not owned:
            raise ProductionDriverError(
                "UNOWNED_CONTAINER", "backend capability is not module-owned"
            )
        stopped = self._docker_run(
            (_DOCKER_EXECUTABLE, "stop", "--time", "1", owned.container_id),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if stopped.returncode != 0 or stopped.stdout.strip() != owned.container_id:
            if self._backend_capability_is_gone(owned):
                self._forget_backend_capability(owned)
                return
            raise ProductionDriverError(
                "BACKEND_CLEANUP_FAILED", "owned backend capability did not stop"
            )
        self._forget_backend_capability(owned)

    def await_backend(self, owned: _OwnedBackendContainerV1, *, deadline_ns: int) -> str:
        last_error: Exception | None = None
        while time.monotonic_ns() < deadline_ns:
            try:
                health = _bounded_http_json(owned.spec.endpoint, "/health", timeout_seconds=2.0)
                if type(health) is dict and cast(dict[object, object], health).get("ok") is True:
                    return _hash_projection(
                        "backend-health",
                        cast(JsonValue, {"container_id": owned.container_id, "health": health}),
                    )
            except Exception as exc:
                last_error = exc
            time.sleep(self._config.health_poll_interval_ms / 1_000)
        raise ProductionDriverError(
            "BACKEND_STARTUP_TIMEOUT", "MobileWorld backend did not become ready"
        ) from last_error

    def attest_backend(self, owned: _OwnedBackendContainerV1) -> str:
        if self._backend_candidates.get(owned.container_id) is not owned:
            raise ProductionDriverError(
                "BACKEND_DISPATCH_IDENTITY_LOST", "backend is not the owned container"
            )
        inspected = self._docker_run(
            (
                _DOCKER_EXECUTABLE,
                "container",
                "inspect",
                "--format",
                "{{.Id}} {{.Name}} {{.Image}} {{.State.Running}}",
                owned.container_id,
            ),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if inspected.returncode != 0 or inspected.stdout.strip() != (
            f"{owned.container_id} /{owned.name} sha256:{self._config.backend_image_id_sha256} true"
        ):
            raise ProductionDriverError(
                "BACKEND_DISPATCH_IDENTITY_LOST",
                "backend container ID/image/running state changed",
            )
        health = _bounded_http_json(owned.spec.endpoint, "/health", timeout_seconds=2.0)
        if type(health) is not dict or cast(dict[object, object], health).get("ok") is not True:
            raise ProductionDriverError("BACKEND_DISPATCH_HEALTH_FAILED", "backend health differs")
        return _hash_projection(
            "backend-dispatch-attestation",
            cast(JsonValue, {"container_id": owned.container_id, "health": health}),
        )

    def stop_backend(self, owned: _OwnedBackendContainerV1) -> None:
        if self._backend_candidates.get(owned.container_id) is not owned:
            raise ProductionDriverError("UNOWNED_CONTAINER", "backend is not module-owned")
        inspected = self._docker_run(
            (
                _DOCKER_EXECUTABLE,
                "container",
                "inspect",
                "--format",
                "{{.Id}} {{.Name}}",
                owned.container_id,
            ),
            timeout_seconds=_RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1,
        )
        if inspected.returncode != 0:
            if self._docker_inspect_confirms_absent(
                inspected, owned.container_id
            ) and self._backend_capability_is_gone(owned):
                self._forget_backend_capability(owned)
                return
            raise ProductionDriverError(
                "BACKEND_OWNERSHIP_MISMATCH", "container identity changed before cleanup"
            )
        if inspected.stdout.strip() != f"{owned.container_id} /{owned.name}":
            raise ProductionDriverError(
                "BACKEND_OWNERSHIP_MISMATCH", "container identity changed before cleanup"
            )
        stopped = self._docker_run(
            (
                _DOCKER_EXECUTABLE,
                "stop",
                "--time",
                str(self._config.shutdown_grace_seconds),
                owned.container_id,
            ),
            timeout_seconds=(
                self._config.shutdown_grace_seconds + _RESOURCE_DOCKER_COMMAND_TIMEOUT_SECONDS_V1
            ),
        )
        if stopped.returncode != 0 or stopped.stdout.strip() != owned.container_id:
            raise ProductionDriverError("BACKEND_CLEANUP_FAILED", "owned container did not stop")
        self._forget_backend_capability(owned)

    def retry_pending_cleanup(self) -> None:
        failures = False
        for pid in tuple(self._partial_models):
            try:
                self._stop_partial_model(pid)
            except Exception:
                failures = True
        for container_id in tuple(self._pending_backend_ids):
            owned = self._backend_candidates.get(container_id)
            if owned is None:
                self._pending_backend_ids.discard(container_id)
                continue
            try:
                self._stop_backend_capability(owned)
            except Exception:
                failures = True
        for name, spec in tuple(self._pending_backend_names.items()):
            try:
                owned = self._recover_backend_candidate(name, spec)
            except Exception:
                failures = True
                continue
            if owned is None:
                failures = True
                continue
            try:
                self._stop_backend_capability(owned)
            except Exception:
                failures = True
        if failures:
            raise ProductionDriverError(
                "RESOURCE_PENDING_CLEANUP_FAILED",
                "partially started resources remain owned for another cleanup attempt",
            )

    def residual_capabilities(self) -> dict[str, JsonValue]:
        partial_models: list[JsonValue] = []
        for pid, partial in sorted(self._partial_models.items()):
            try:
                identity = _read_owned_process_identity(pid)
                identity_projection: JsonValue | None = cast(
                    JsonValue,
                    {
                        "process_group_id": identity.process_group_id,
                        "session_id": identity.session_id,
                        "starttime_ticks": identity.starttime_ticks,
                        "uid": identity.uid,
                    },
                )
            except ProductionDriverError:
                identity_projection = None
            partial_models.append(
                cast(
                    JsonValue,
                    {
                        "identity": identity_projection,
                        "pid": pid,
                        "running": partial.process.poll() is None,
                    },
                )
            )
        admitted_models = [
            cast(
                JsonValue,
                {
                    "pid": owned.identity.pid,
                    "process_group_id": owned.identity.process_group_id,
                    "session_id": owned.identity.session_id,
                    "starttime_ticks": owned.identity.starttime_ticks,
                    "uid": owned.identity.uid,
                },
            )
            for _, owned in sorted(self._models.items())
        ]
        backend_candidates = [
            cast(JsonValue, {"container_id": container_id, "name": owned.name})
            for container_id, owned in sorted(self._backend_candidates.items())
        ]
        return {
            "admitted_model_processes": admitted_models,
            "backend_candidates": backend_candidates,
            "partial_model_processes": partial_models,
            "pending_backend_ids": cast(JsonValue, sorted(self._pending_backend_ids)),
            "pending_backend_names": cast(JsonValue, sorted(self._pending_backend_names)),
        }


class _CpuRecordingResourceSystemV1:
    """Exact in-memory system used only to test fixed production sequencing."""

    __slots__ = (
        "_cleanup",
        "_commands",
        "_dispatch",
        "_fault",
        "_health",
        "_active_models",
        "_next_pid",
        "_pending_cleanup",
        "_shared_attestation_count",
        "_stop_fault_consumed",
    )

    def __init__(self, fault: CpuResourceLifecycleFaultV1, *, seal: object) -> None:
        if seal is not _MODULE_SEAL or type(fault) is not CpuResourceLifecycleFaultV1:
            raise PermissionError("CPU recording resource system is module-owned")
        self._commands: list[tuple[str, ...]] = []
        self._health: list[str] = []
        self._cleanup: list[str] = []
        self._dispatch: list[str] = []
        self._next_pid = 10_000
        self._active_models: dict[int, _OwnedModelProcessV1] = {}
        self._fault = fault
        self._pending_cleanup: list[str] = []
        self._shared_attestation_count = 0
        self._stop_fault_consumed = False

    def attest_runtime(self, config: ProductionRuntimeConfigV1, *, source_commit: str) -> str:
        git_prefix = (
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
        )
        self._commands.extend(
            (
                (*git_prefix, "rev-parse", "HEAD"),
                (*git_prefix, "status", "--porcelain=v1", "--untracked-files=all"),
                (*git_prefix, "rev-parse", f"{source_commit}:MobileWorld/src"),
                (
                    config.vllm_python_executable,
                    "-P",
                    "-B",
                    "-c",
                    "import importlib.metadata as m; print(m.version('vllm'))",
                ),
                (
                    _DOCKER_EXECUTABLE,
                    "network",
                    "inspect",
                    "--format",
                    "{{.Name}}",
                    _DOCKER_NETWORK,
                ),
                (
                    _DOCKER_EXECUTABLE,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    _MOBILEWORLD_BACKEND_IMAGE,
                ),
            )
        )
        return _runtime_attestation_sha256(
            config,
            source_commit=source_commit,
            source_tree_git_sha1=_hash_projection("cpu-source-tree-git-sha1", source_commit)[:40],
        )

    def attest_gpu_idle(self, gpu_index: int) -> str:
        identity_command = (
            _NVIDIA_SMI_EXECUTABLE,
            f"--id={gpu_index}",
            "--query-gpu=index,uuid",
            "--format=csv,noheader,nounits",
        )
        process_command = (
            _NVIDIA_SMI_EXECUTABLE,
            f"--id={gpu_index}",
            "--query-compute-apps=gpu_uuid,pid",
            "--format=csv,noheader,nounits",
        )
        self._commands.extend((identity_command, process_command))
        return _hash_projection(
            "cpu-gpu-idle-attestation",
            cast(
                JsonValue,
                {
                    "gpu_index": gpu_index,
                    "gpu_uuid": f"gpu-{gpu_index:032x}",
                    "identity_query": list(identity_command),
                    "process_query": list(process_command),
                },
            ),
        )

    def attest_gpu_shared_capacity(
        self,
        gpu_index: int,
        *,
        minimum_free_memory_mib: int,
    ) -> ProductionSharedGpuAttestationV1:
        identity_command = (
            _NVIDIA_SMI_EXECUTABLE,
            f"--id={gpu_index}",
            "--query-gpu=index,uuid,memory.total,memory.free,memory.used,memory.reserved,"
            "utilization.gpu,utilization.memory",
            "--format=csv,noheader,nounits",
        )
        process_command = (
            _NVIDIA_SMI_EXECUTABLE,
            f"--id={gpu_index}",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        )
        self._commands.extend((identity_command, process_command))
        self._shared_attestation_count += 1
        active = tuple(self._active_models.values())
        free_memory_mib = 90_169 if not active else 56_212
        if (
            self._fault is CpuResourceLifecycleFaultV1.SHARED_GPU_CAPACITY_BELOW_MINIMUM
            and self._shared_attestation_count == 1
        ) or (
            self._fault is CpuResourceLifecycleFaultV1.HANDOFF_GPU_CAPACITY_BELOW_MINIMUM
            and self._shared_attestation_count == 3
        ):
            free_memory_mib = 50_000
        total_memory_mib = 143_771
        reserved_memory_mib = 614
        processes = [
            SharedGpuProcessEvidenceV1(
                pid=42_000,
                process_group_id=42_000,
                session_id=42_000,
                starttime_ticks=4_200_000,
                uid=1_001,
                user="cpu-peer-a",
                used_gpu_memory_mib=31_346,
            ),
            SharedGpuProcessEvidenceV1(
                pid=42_001,
                process_group_id=42_001,
                session_id=42_001,
                starttime_ticks=4_200_100,
                uid=1_002,
                user="cpu-peer-b",
                used_gpu_memory_mib=22_256,
            ),
        ]
        processes.extend(
            SharedGpuProcessEvidenceV1(
                pid=item.identity.pid,
                process_group_id=item.identity.process_group_id,
                session_id=item.identity.session_id,
                starttime_ticks=item.identity.starttime_ticks,
                uid=item.identity.uid,
                user="cpu-owner",
                used_gpu_memory_mib=33_957,
            )
            for item in active
        )
        if (
            self._fault is CpuResourceLifecycleFaultV1.SHARED_GPU_TENANT_DRIFT
            and self._shared_attestation_count == 3
        ) or (
            self._fault is CpuResourceLifecycleFaultV1.SHARED_GPU_TENANT_DRIFT_PERSISTS
            and self._shared_attestation_count >= 3
        ):
            processes.append(
                SharedGpuProcessEvidenceV1(
                    pid=42_999,
                    process_group_id=42_999,
                    session_id=42_999,
                    starttime_ticks=4_299_900,
                    uid=os.geteuid(),
                    user="cpu-owner",
                    used_gpu_memory_mib=1_024,
                )
            )
        return ProductionSharedGpuAttestationV1(
            gpu_index=gpu_index,
            gpu_uuid=f"GPU-{gpu_index:08x}-0000-0000-0000-000000000000",
            total_memory_mib=total_memory_mib,
            free_memory_mib=free_memory_mib,
            used_memory_mib=total_memory_mib - free_memory_mib - reserved_memory_mib,
            reserved_memory_mib=reserved_memory_mib,
            gpu_utilization_percent=0 if not active else 93,
            memory_utilization_percent=(
                (total_memory_mib - free_memory_mib) * 100 // total_memory_mib
            ),
            minimum_free_memory_mib=minimum_free_memory_mib,
            processes=tuple(processes),
        )

    def start_model(self, spec: ProductionCommandSpecV1, *, log_label: str) -> _OwnedModelProcessV1:
        del log_label
        self._commands.append(spec.argv)
        pid = self._next_pid
        self._next_pid += 1
        if self._fault in {
            CpuResourceLifecycleFaultV1.MODEL_PARTIAL_START,
            CpuResourceLifecycleFaultV1.MODEL_PARTIAL_CLEANUP_ONCE,
        } or (
            self._fault is CpuResourceLifecycleFaultV1.MAI_PARTIAL_START and spec.kind == "VLLM_MAI"
        ):
            self._pending_cleanup.append(f"partial-pid:{pid}")
            raise ProductionDriverError(
                "MODEL_PARTIAL_START_RECOVERABLE", "CPU partial child awaits cleanup"
            )
        owned = _OwnedModelProcessV1(
            identity=OwnedProcessIdentityV1(
                pid=pid,
                process_group_id=pid,
                session_id=pid,
                starttime_ticks=pid * 100,
                uid=os.geteuid(),
            ),
            process=None,
            spec=spec,
            stdout_handle=None,
            stderr_handle=None,
        )
        self._active_models[pid] = owned
        return owned

    def await_model(self, owned: _OwnedModelProcessV1, *, deadline_ns: int) -> str:
        del deadline_ns
        self._health.extend(f"{owned.spec.endpoint}{path}" for path in owned.spec.health_paths)
        return _hash_projection(
            "cpu-model-health",
            cast(JsonValue, production_command_spec_projection(owned.spec)),
        )

    def attest_model(self, owned: _OwnedModelProcessV1) -> str:
        self._dispatch.append(f"model:{owned.spec.kind}:{owned.identity.starttime_ticks}")
        if self._fault is CpuResourceLifecycleFaultV1.DISPATCH_MODEL_IDENTITY:
            raise ProductionDriverError("MODEL_DISPATCH_IDENTITY_LOST", "CPU model identity drift")
        return self.await_model(owned, deadline_ns=time.monotonic_ns() + 1_000_000_000)

    def stop_model(self, owned: _OwnedModelProcessV1) -> ProductionModelStopEvidenceV1:
        if (
            self._fault
            in {
                CpuResourceLifecycleFaultV1.MODEL_STOP_ONCE,
                CpuResourceLifecycleFaultV1.HANDOFF_MODEL_REAP_UNCONFIRMED,
            }
            and not self._stop_fault_consumed
        ):
            self._stop_fault_consumed = True
            code = (
                "MODEL_REAP_UNCONFIRMED"
                if self._fault is CpuResourceLifecycleFaultV1.HANDOFF_MODEL_REAP_UNCONFIRMED
                else "MODEL_CLEANUP_FAILED"
            )
            raise ProductionDriverError(code, "CPU one-shot cleanup failure")
        self._cleanup.append(f"pid:{owned.identity.pid}")
        self._active_models.pop(owned.identity.pid, None)
        return ProductionModelStopEvidenceV1(
            host=(PilotHostV1.QWEN3_VL if owned.spec.kind == "VLLM_QWEN" else PilotHostV1.MAI_UI),
            process=owned.identity,
            endpoint=owned.spec.endpoint,
            leader_reaped=True,
            session_members_remaining=0,
            port_available=True,
        )

    def start_backend(self, spec: ProductionCommandSpecV1) -> _OwnedBackendContainerV1:
        self._commands.append(spec.argv)
        owned = _OwnedBackendContainerV1(
            container_id=_hash_projection(
                "cpu-container-id", cast(JsonValue, production_command_spec_projection(spec))
            ),
            name=spec.argv[spec.argv.index("--name") + 1],
            spec=spec,
        )
        if self._fault in {
            CpuResourceLifecycleFaultV1.BACKEND_START_TIMEOUT,
            CpuResourceLifecycleFaultV1.BACKEND_OWNERSHIP_MISMATCH,
        }:
            self._pending_cleanup.append(f"container:{owned.container_id}")
            code = (
                "BACKEND_PARTIAL_START_RECOVERABLE"
                if self._fault is CpuResourceLifecycleFaultV1.BACKEND_START_TIMEOUT
                else "BACKEND_OWNERSHIP_MISMATCH_RECOVERABLE"
            )
            raise ProductionDriverError(code, "CPU backend candidate awaits cleanup")
        return owned

    def await_backend(self, owned: _OwnedBackendContainerV1, *, deadline_ns: int) -> str:
        del deadline_ns
        self._health.append(f"{owned.spec.endpoint}/health")
        return _hash_projection(
            "cpu-backend-health", cast(JsonValue, {"container_id": owned.container_id})
        )

    def attest_backend(self, owned: _OwnedBackendContainerV1) -> str:
        self._dispatch.append(f"backend:{owned.container_id}")
        if self._fault is CpuResourceLifecycleFaultV1.DISPATCH_BACKEND_IDENTITY:
            raise ProductionDriverError(
                "BACKEND_DISPATCH_IDENTITY_LOST", "CPU backend identity drift"
            )
        return self.await_backend(owned, deadline_ns=time.monotonic_ns() + 1_000_000_000)

    def stop_backend(self, owned: _OwnedBackendContainerV1) -> None:
        self._cleanup.append(f"container:{owned.container_id}")

    def retry_pending_cleanup(self) -> None:
        if (
            self._fault is CpuResourceLifecycleFaultV1.MODEL_PARTIAL_CLEANUP_ONCE
            and not self._stop_fault_consumed
        ):
            self._stop_fault_consumed = True
            raise ProductionDriverError(
                "RESOURCE_PENDING_CLEANUP_FAILED",
                "CPU partial ownership remains for one cleanup retry",
            )
        self._cleanup.extend(self._pending_cleanup)
        self._pending_cleanup.clear()

    def residual_capabilities(self) -> dict[str, JsonValue]:
        return {
            "admitted_model_processes": [],
            "backend_candidates": [],
            "partial_model_processes": [
                cast(JsonValue, {"capability": item})
                for item in self._pending_cleanup
                if item.startswith("partial-pid:")
            ],
            "pending_backend_ids": [
                item.removeprefix("container:")
                for item in self._pending_cleanup
                if item.startswith("container:")
            ],
            "pending_backend_names": [],
        }

    @property
    def trace(self) -> CpuResourceLifecycleTraceV1:
        return CpuResourceLifecycleTraceV1(
            commands=tuple(self._commands),
            health_endpoints=tuple(self._health),
            cleanup_targets=tuple(self._cleanup),
            dispatch_attestations=tuple(self._dispatch),
            pending_cleanup_count=len(self._pending_cleanup),
        )


type _ResourceSystemV1 = _PosixProductionResourceSystemV1 | _CpuRecordingResourceSystemV1


class ProductionResourceLifecycleAdapterV1:
    """Exact Qwen/MAI vLLM plus MobileWorld container lifecycle adapter."""

    __slots__ = (
        "_backend",
        "_config",
        "_disabled_hosts",
        "_evidence",
        "_failure_evidence",
        "_handoff_evidence",
        "_handoff_failure_evidence",
        "_last_dispatch_evidence",
        "_last_dispatch_failure_evidence",
        "_gpu_leases",
        "_lock",
        "_manifest_sha256",
        "_model_specs",
        "_models",
        "_resources",
        "_shared_gpu_baseline",
        "_cleanup_success_evidence",
        "_cleanup_failure_evidence",
        "_last_shared_cleanup_attestation",
        "_reclaimed_cleanup_outcome",
        "_reclaimed_cleanup_values",
        "_stop_evidence",
        "_system",
    )

    def __init__(
        self,
        config: ProductionRuntimeConfigV1,
        *,
        system: _ResourceSystemV1,
        seal: object,
    ) -> None:
        if (
            seal is not _MODULE_SEAL
            or type(config) is not ProductionRuntimeConfigV1
            or type(system) not in {_PosixProductionResourceSystemV1, _CpuRecordingResourceSystemV1}
        ):
            raise PermissionError("resource lifecycle adapter is module-owned")
        self._config = config
        self._system = system
        self._backend: _OwnedBackendContainerV1 | None = None
        self._models: dict[PilotHostV1, _OwnedModelProcessV1] = {}
        self._model_specs: dict[PilotHostV1, ProductionCommandSpecV1] = {}
        self._resources: dict[PilotHostV1, SnapshotResourceV1] = {}
        self._gpu_leases: dict[PilotHostV1, _ExclusiveGpuLeaseV1 | _CpuExclusiveGpuLeaseV1] = {}
        self._disabled_hosts: set[PilotHostV1] = set()
        self._manifest_sha256: str | None = None
        self._evidence: ProductionResourceStageEvidenceV1 | None = None
        self._failure_evidence: bytes | None = None
        self._handoff_evidence: ProductionModelHandoffEvidenceV1 | None = None
        self._handoff_failure_evidence: bytes | None = None
        self._last_dispatch_evidence: bytes | None = None
        self._last_dispatch_failure_evidence: bytes | None = None
        self._cleanup_success_evidence: bytes | None = None
        self._cleanup_failure_evidence: bytes | None = None
        self._last_shared_cleanup_attestation: ProductionSharedGpuAttestationV1 | None = None
        self._reclaimed_cleanup_outcome: bytes | None = None
        self._reclaimed_cleanup_values: (
            tuple[ProductionSharedGpuAttestationV1 | None, tuple[str, ...], str | None] | None
        ) = None
        self._shared_gpu_baseline: ProductionSharedGpuAttestationV1 | None = None
        self._stop_evidence: list[ProductionModelStopEvidenceV1] = []
        self._lock = threading.RLock()

    def _cache_reclaimed_cleanup(
        self,
        context: StageAdapterContextV1,
        values: tuple[ProductionSharedGpuAttestationV1 | None, tuple[str, ...], str | None],
    ) -> None:
        final_attestation, lease_sha256s, backend_container_id = values
        residual = self._system.residual_capabilities()
        projection: dict[str, JsonValue] = {
            "backend_container_id": backend_container_id,
            "final_shared_gpu_attestation": (
                None
                if final_attestation is None
                else production_shared_gpu_attestation_projection(final_attestation)
            ),
            "gpu_lease_sha256s": list(lease_sha256s),
            "manifest_sha256": context.manifest_sha256,
            "resource_topology": self._config.resource_topology.value,
            "residual_capabilities": residual,
            "runtime_config_sha256": production_runtime_config_sha256(self._config),
            "sequence_execution_scope": context.sequence_execution_scope,
            "sequence_scope_authority_sha256": context.sequence_scope_authority_sha256,
            "status": "RECLAIMED",
            "stopped_models": [
                production_model_stop_evidence_projection(item) for item in self._stop_evidence
            ],
        }
        self._reclaimed_cleanup_values = values
        self._reclaimed_cleanup_outcome = canonical_json_bytes(
            cast(
                JsonValue,
                {
                    "domain": "production-resource-reclaimed-cleanup-outcome",
                    "schema_version": PRODUCTION_RESOURCE_CLEANUP_EVIDENCE_SCHEMA_VERSION_V1,
                    "value": projection,
                },
            )
        )

    def _resource_failure_preimage(
        self,
        context: StageAdapterContextV1,
        *,
        failure_code: str,
        status: str,
        cleanup_status: str,
        cleanup_failure_code: str | None = None,
    ) -> bytes:
        residual = self._system.residual_capabilities()
        reclaimed = (
            None
            if self._reclaimed_cleanup_outcome is None
            else cast(JsonValue, json.loads(self._reclaimed_cleanup_outcome))
        )
        return canonical_json_bytes(
            cast(
                JsonValue,
                {
                    "backend_container_id": (
                        None if self._backend is None else self._backend.container_id
                    ),
                    "cleanup_failure_code": cleanup_failure_code,
                    "cleanup_status": cleanup_status,
                    "completed_model_processes": [
                        {
                            "pid": item.identity.pid,
                            "process_group_id": item.identity.process_group_id,
                            "session_id": item.identity.session_id,
                            "starttime_ticks": item.identity.starttime_ticks,
                            "uid": item.identity.uid,
                        }
                        for item in self._models.values()
                    ],
                    "failure_code": failure_code,
                    "gpu_lease_sha256s": [
                        lease.lease_sha256 for lease in self._gpu_leases.values()
                    ],
                    "manifest_sha256": context.manifest_sha256,
                    "resource_topology": self._config.resource_topology.value,
                    "reclaimed_cleanup_outcome": reclaimed,
                    "reclaimed_cleanup_outcome_sha256": (
                        None
                        if self._reclaimed_cleanup_outcome is None
                        else hashlib.sha256(self._reclaimed_cleanup_outcome).hexdigest()
                    ),
                    "runtime_config_sha256": production_runtime_config_sha256(self._config),
                    "sequence_execution_scope": context.sequence_execution_scope,
                    "sequence_scope_authority_sha256": (context.sequence_scope_authority_sha256),
                    "shared_gpu_baseline": (
                        None
                        if self._shared_gpu_baseline is None
                        else production_shared_gpu_attestation_projection(self._shared_gpu_baseline)
                    ),
                    "residual_capabilities": residual,
                    "residual_capabilities_sha256": _hash_projection(
                        "production-resource-residual-capabilities",
                        cast(JsonValue, residual),
                    ),
                    "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                    "stage": RunStageV1.RESOURCE_PREFLIGHT.value,
                    "status": status,
                },
            )
        )

    def prepare(
        self,
        resources: tuple[SnapshotResourceV1, ...],
        context: StageAdapterContextV1,
    ) -> AdapterStageResultV1:
        trusted_context = _snapshot_context(context)
        if self._config.resource_topology is (
            ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        ):
            if trusted_context.sequence_execution_scope != "R24_LIVE_SMOKE_ONLY":
                raise ProductionDriverError(
                    "RESOURCE_SCOPE_UNAUTHORIZED",
                    "shared sequential resources require smoke-only sequence authority",
                )
            _require_sha256(
                trusted_context.sequence_scope_authority_sha256,
                "sequence_scope_authority_sha256",
            )
        if self._manifest_sha256 is None:
            self._manifest_sha256 = trusted_context.manifest_sha256
        elif self._manifest_sha256 != trusted_context.manifest_sha256:
            raise ProductionDriverError(
                "RESOURCE_BINDING_MISMATCH", "resource manifest authority differs"
            )
        self._failure_evidence = canonical_json_bytes(
            cast(
                JsonValue,
                {
                    "completed_model_processes": [],
                    "failure_code": "RESOURCE_ADAPTER_FAILED",
                    "manifest_sha256": trusted_context.manifest_sha256,
                    "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                    "stage": RunStageV1.RESOURCE_PREFLIGHT.value,
                    "status": "FAILED",
                },
            )
        )
        if self._backend is not None or self._models or self._evidence is not None:
            raise ProductionDriverError("RESOURCES_ALREADY_PREPARED", "resources are not fresh")
        if type(resources) is not tuple:
            raise ProductionDriverError("UNTRUSTED_TYPE", "resource matrix must be an exact tuple")
        trusted_resources = tuple(_snapshot_resource(item) for item in resources)
        if tuple(item.host for item in trusted_resources) != (
            PilotHostV1.QWEN3_VL,
            PilotHostV1.MAI_UI,
        ):
            raise ProductionDriverError("RESOURCE_MATRIX_MISMATCH", "resource order differs")
        if self._config.backend_port in {
            _exact_loopback_endpoint(item.actor_endpoint, permitted_paths=frozenset({"", "/v1"}))[1]
            for item in trusted_resources
        }:
            raise ProductionDriverError(
                "RESOURCE_PORT_COLLISION", "backend and actor ports overlap"
            )
        if self._config.startup_timeout_seconds * 1_000 > trusted_context.remaining_wall_time_ms:
            raise ProductionDriverError(
                "RESOURCE_BUDGET_EXCEEDED", "resource startup bound exceeds stage authority"
            )
        if self._disabled_hosts:
            raise ProductionDriverError(
                "HOST_DISABLED", "a host kill/disable gate was set before resource startup"
            )
        self._resources = {item.host: item for item in trusted_resources}

        def require_dispatch_authority() -> None:
            if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                raise ProductionDriverError(
                    "OWNER_AUTHORITY_EXPIRED", "resource dispatch authority elapsed"
                )

        require_dispatch_authority()
        # Preflight evidence is not a lease on mutable model directories.  Rehash
        # both complete trees immediately before any Docker/process operation.
        for resource in trusted_resources:
            _attest_snapshot_resource(resource)
            require_dispatch_authority()
        try:
            lease_hosts = (
                (PilotHostV1.QWEN3_VL,)
                if self._config.resource_topology
                is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
                else (PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI)
            )
            for host in lease_hosts:
                gpu_index = (
                    self._config.qwen_gpu_index
                    if host is PilotHostV1.QWEN3_VL
                    else self._config.mai_gpu_index
                )
                self._gpu_leases[host] = (
                    _ExclusiveGpuLeaseV1(gpu_index)
                    if type(self._system) is _PosixProductionResourceSystemV1
                    else _CpuExclusiveGpuLeaseV1(gpu_index, seal=_MODULE_SEAL)
                )
        except Exception as exc:
            lease_sha256s = tuple(lease.lease_sha256 for lease in self._gpu_leases.values())
            for lease in self._gpu_leases.values():
                lease.close()
            self._gpu_leases.clear()
            self._resources.clear()
            self._cache_reclaimed_cleanup(
                trusted_context,
                (None, lease_sha256s, None),
            )
            self._failure_evidence = self._resource_failure_preimage(
                trusted_context,
                failure_code=_exception_code(exc, "RESOURCE_ATTESTATION_FAILED"),
                status="FAILED",
                cleanup_status="RECLAIMED",
            )
            raise
        deadline_ns = min(
            trusted_context.authority_deadline_monotonic_ns,
            time.monotonic_ns() + self._config.startup_timeout_seconds * 1_000_000_000,
        )
        try:
            require_dispatch_authority()
            runtime_attestation_sha256 = self._system.attest_runtime(
                self._config,
                source_commit=trusted_context.source_commit,
            )
            backend_spec = _backend_command_spec(
                self._config, manifest_sha256=trusted_context.manifest_sha256
            )
            model_specs = tuple(
                _vllm_command_spec(
                    item,
                    gpu_index=(
                        self._config.qwen_gpu_index
                        if item.host is PilotHostV1.QWEN3_VL
                        else self._config.mai_gpu_index
                    ),
                    config=self._config,
                )
                for item in trusted_resources
            )
            self._model_specs = {
                resource.host: spec
                for resource, spec in zip(trusted_resources, model_specs, strict=True)
            }
        except Exception as exc:
            lease_sha256s = tuple(lease.lease_sha256 for lease in self._gpu_leases.values())
            for lease in self._gpu_leases.values():
                lease.close()
            self._gpu_leases.clear()
            self._resources.clear()
            self._cache_reclaimed_cleanup(
                trusted_context,
                (None, lease_sha256s, None),
            )
            self._failure_evidence = self._resource_failure_preimage(
                trusted_context,
                failure_code=_exception_code(exc, "RESOURCE_ATTESTATION_FAILED"),
                status="FAILED",
                cleanup_status="RECLAIMED",
            )
            raise
        try:
            require_dispatch_authority()
            self._backend = self._system.start_backend(backend_spec)
            require_dispatch_authority()
            backend_health = self._system.await_backend(self._backend, deadline_ns=deadline_ns)
            model_health: list[str] = []
            gpu_idle_attestations: list[str] = []
            shared_gpu_attestations: list[ProductionSharedGpuAttestationV1] = []
            startup_pairs = tuple(zip(trusted_resources, model_specs, strict=True))
            if (
                self._config.resource_topology
                is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
            ):
                startup_pairs = startup_pairs[:1]
            for resource, spec in startup_pairs:
                require_dispatch_authority()
                gpu_index = (
                    self._config.qwen_gpu_index
                    if resource.host is PilotHostV1.QWEN3_VL
                    else self._config.mai_gpu_index
                )
                if (
                    self._config.resource_topology
                    is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
                ):
                    baseline = self._system.attest_gpu_shared_capacity(
                        gpu_index,
                        minimum_free_memory_mib=self._config.minimum_free_gpu_memory_mib,
                    )
                    self._shared_gpu_baseline = baseline
                    shared_gpu_attestations.append(baseline)
                    _require_shared_gpu_capacity(baseline)
                else:
                    gpu_idle_attestations.append(self._system.attest_gpu_idle(gpu_index))
                require_dispatch_authority()
                owned = self._system.start_model(
                    spec, log_label=f"{trusted_context.run_id}-{resource.host.value.lower()}"
                )
                self._models[resource.host] = owned
                model_health.append(self._system.await_model(owned, deadline_ns=deadline_ns))
                # Bind the loaded/READY model to the same immutable tree that was
                # checked before Popen; a mutable snapshot may not race startup.
                _attest_snapshot_resource(resource)
                require_dispatch_authority()
                if (
                    self._config.resource_topology
                    is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
                ):
                    assert self._shared_gpu_baseline is not None
                    ready_attestation = self._system.attest_gpu_shared_capacity(
                        gpu_index,
                        minimum_free_memory_mib=0,
                    )
                    _validate_shared_gpu_tenants(
                        ready_attestation,
                        baseline=self._shared_gpu_baseline,
                        owned_identity=owned.identity,
                    )
                    shared_gpu_attestations.append(ready_attestation)
        except Exception as exc:
            failure_code = getattr(exc, "code", "RESOURCE_ADAPTER_FAILED")
            if type(failure_code) is not str:
                failure_code = "RESOURCE_ADAPTER_FAILED"
            self._failure_evidence = self._resource_failure_preimage(
                trusted_context,
                failure_code=failure_code,
                status="FAILED",
                cleanup_status="PENDING",
            )
            try:
                cleanup_values = self._cleanup_owned()
                final_attestation, _, _ = cleanup_values
                if final_attestation is not None:
                    self._last_shared_cleanup_attestation = final_attestation
                self._cache_reclaimed_cleanup(trusted_context, cleanup_values)
            except Exception as cleanup_exc:
                cleanup_failure_code = getattr(cleanup_exc, "code", "RESOURCE_CLEANUP_FAILED")
                if type(cleanup_failure_code) is not str:
                    cleanup_failure_code = "RESOURCE_CLEANUP_FAILED"
                self._failure_evidence = self._resource_failure_preimage(
                    trusted_context,
                    failure_code=failure_code,
                    status="FAILED_CLEANUP_RETRY_REQUIRED",
                    cleanup_status="RETRY_REQUIRED",
                    cleanup_failure_code=cleanup_failure_code,
                )
                raise ProductionDriverError(
                    "RESOURCE_CLEANUP_FAILED",
                    "resource startup failed and owned cleanup requires retry",
                ) from cleanup_exc
            self._failure_evidence = self._resource_failure_preimage(
                trusted_context,
                failure_code=failure_code,
                status="FAILED",
                cleanup_status="RECLAIMED",
            )
            raise
        assert self._backend is not None
        evidence = ProductionResourceStageEvidenceV1(
            manifest_sha256=trusted_context.manifest_sha256,
            runtime_config_sha256=production_runtime_config_sha256(self._config),
            runtime_attestation_sha256=runtime_attestation_sha256,
            backend_command_sha256=production_command_spec_sha256(backend_spec),
            backend_container_id=self._backend.container_id,
            backend_health_sha256=backend_health,
            model_command_sha256s=cast(
                tuple[str, str], tuple(production_command_spec_sha256(item) for item in model_specs)
            ),
            model_processes=tuple(self._models[item.host].identity for item, _ in startup_pairs),
            model_health_sha256s=tuple(model_health),
            gpu_lease_sha256s=tuple(lease.lease_sha256 for lease in self._gpu_leases.values()),
            gpu_idle_attestation_sha256s=tuple(gpu_idle_attestations),
            shared_gpu_attestations=tuple(shared_gpu_attestations),
            active_hosts=tuple(item.host for item, _ in startup_pairs),
            resource_topology=self._config.resource_topology,
            vllm_gpu_memory_utilization=self._config.vllm_gpu_memory_utilization,
            minimum_free_gpu_memory_mib=self._config.minimum_free_gpu_memory_mib,
            sequence_execution_scope=trusted_context.sequence_execution_scope,
            sequence_scope_authority_sha256=(trusted_context.sequence_scope_authority_sha256),
        )
        self._evidence = evidence
        evidence_preimage = _production_resource_stage_evidence_preimage(evidence)
        return AdapterStageResultV1(
            stage=RunStageV1.RESOURCE_PREFLIGHT,
            manifest_sha256=trusted_context.manifest_sha256,
            evidence_sha256=production_resource_stage_evidence_sha256(evidence),
            evidence_preimage=evidence_preimage,
            actor_calls=0,
            openai_calls=0,
            actor_actions=0,
            cost_usd_micros=0,
            completed_units=("resources",),
            provider_final_request_proven=False,
        )

    def handoff_to_mai(self, context: StageAdapterContextV1) -> AdapterStageResultV1:
        """Replace Qwen with MAI on the same leased shared GPU exactly once."""

        trusted_context = _snapshot_context(context)
        progress: dict[str, JsonValue] = {
            "manifest_sha256": trusted_context.manifest_sha256,
            "resource_topology": self._config.resource_topology.value,
            "runtime_config_sha256": production_runtime_config_sha256(self._config),
            "sequence_execution_scope": trusted_context.sequence_execution_scope,
            "sequence_scope_authority_sha256": (trusted_context.sequence_scope_authority_sha256),
            "source_host": PilotHostV1.QWEN3_VL.value,
            "status": "STARTED",
            "target_host": PilotHostV1.MAI_UI.value,
        }
        if self._config.resource_topology is not (
            ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        ):
            progress["status"] = "NOT_REQUIRED_CONCURRENT"
            preimage = canonical_json_bytes(
                cast(
                    JsonValue,
                    {
                        "domain": "production-model-handoff-evidence",
                        "schema_version": PRODUCTION_MODEL_HANDOFF_EVIDENCE_SCHEMA_VERSION_V1,
                        "value": progress,
                    },
                )
            )
            return AdapterStageResultV1(
                stage=RunStageV1.MAI_LIVE_SMOKE,
                manifest_sha256=trusted_context.manifest_sha256,
                evidence_sha256=hashlib.sha256(preimage).hexdigest(),
                evidence_preimage=preimage,
                actor_calls=0,
                openai_calls=0,
                actor_actions=0,
                cost_usd_micros=0,
                completed_units=("resource-handoff:not-required-concurrent",),
                provider_final_request_proven=False,
            )
        if (
            trusted_context.sequence_execution_scope != "R24_LIVE_SMOKE_ONLY"
            or self._manifest_sha256 != trusted_context.manifest_sha256
            or self._evidence is None
            or self._evidence.sequence_scope_authority_sha256
            != trusted_context.sequence_scope_authority_sha256
        ):
            raise ProductionDriverError(
                "RESOURCE_BINDING_MISMATCH", "shared handoff scope or manifest differs"
            )
        _require_sha256(
            trusted_context.sequence_scope_authority_sha256,
            "sequence_scope_authority_sha256",
        )
        with self._lock:
            if (
                self._handoff_evidence is not None
                or self._shared_gpu_baseline is None
                or set(self._models) != {PilotHostV1.QWEN3_VL}
                or set(self._gpu_leases) != {PilotHostV1.QWEN3_VL}
                or PilotHostV1.MAI_UI not in self._model_specs
                or PilotHostV1.MAI_UI in self._disabled_hosts
            ):
                raise ProductionDriverError(
                    "HANDOFF_INVALID_STATE", "Qwen-to-MAI handoff state differs"
                )
            if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                raise ProductionDriverError(
                    "OWNER_AUTHORITY_EXPIRED", "handoff owner authority elapsed"
                )
            qwen = self._models[PilotHostV1.QWEN3_VL]
            lease = self._gpu_leases[PilotHostV1.QWEN3_VL]
            post_stop: ProductionSharedGpuAttestationV1 | None = None
            target: _OwnedModelProcessV1 | None = None
            target_health: str | None = None
            target_ready: ProductionSharedGpuAttestationV1 | None = None
            source_stop: ProductionModelStopEvidenceV1 | None = None
            try:
                source_stop = self._system.stop_model(qwen)
                self._stop_evidence.append(source_stop)
                self._models.pop(PilotHostV1.QWEN3_VL)
                progress["source_stop"] = cast(
                    JsonValue, production_model_stop_evidence_projection(source_stop)
                )
                if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                    raise ProductionDriverError(
                        "OWNER_AUTHORITY_EXPIRED", "handoff owner authority elapsed"
                    )
                post_stop = self._system.attest_gpu_shared_capacity(
                    self._config.qwen_gpu_index,
                    minimum_free_memory_mib=self._config.minimum_free_gpu_memory_mib,
                )
                if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                    raise ProductionDriverError(
                        "OWNER_AUTHORITY_EXPIRED",
                        "handoff owner authority elapsed after Qwen-stop GPU attestation",
                    )
                progress["post_stop_shared_gpu_attestation"] = cast(
                    JsonValue, production_shared_gpu_attestation_projection(post_stop)
                )
                _require_shared_gpu_capacity(post_stop)
                _validate_shared_gpu_tenants(
                    post_stop,
                    baseline=self._shared_gpu_baseline,
                    owned_identity=None,
                )
                mai_resource = self._resources[PilotHostV1.MAI_UI]
                target_snapshot_attestation_sha256 = _attest_snapshot_resource(mai_resource)
                progress["target_snapshot_attestation_sha256"] = target_snapshot_attestation_sha256
                target_spec = self._model_specs[PilotHostV1.MAI_UI]
                if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                    raise ProductionDriverError(
                        "OWNER_AUTHORITY_EXPIRED",
                        "handoff owner authority elapsed before MAI start",
                    )
                target = self._system.start_model(
                    target_spec,
                    log_label=f"{trusted_context.run_id}-{PilotHostV1.MAI_UI.value.lower()}",
                )
                self._models[PilotHostV1.MAI_UI] = target
                progress["target_process"] = cast(
                    JsonValue,
                    {
                        "pid": target.identity.pid,
                        "process_group_id": target.identity.process_group_id,
                        "session_id": target.identity.session_id,
                        "starttime_ticks": target.identity.starttime_ticks,
                        "uid": target.identity.uid,
                    },
                )
                if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                    raise ProductionDriverError(
                        "OWNER_AUTHORITY_EXPIRED",
                        "handoff owner authority elapsed after MAI start",
                    )
                deadline_ns = min(
                    trusted_context.authority_deadline_monotonic_ns,
                    time.monotonic_ns() + self._config.startup_timeout_seconds * 1_000_000_000,
                )
                target_health = self._system.await_model(target, deadline_ns=deadline_ns)
                if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                    raise ProductionDriverError(
                        "OWNER_AUTHORITY_EXPIRED",
                        "handoff owner authority elapsed during MAI readiness",
                    )
                progress["target_health_sha256"] = target_health
                target_snapshot_attestation_after_sha256 = _attest_snapshot_resource(mai_resource)
                if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                    raise ProductionDriverError(
                        "OWNER_AUTHORITY_EXPIRED",
                        "handoff owner authority elapsed during MAI snapshot re-attestation",
                    )
                if target_snapshot_attestation_after_sha256 != target_snapshot_attestation_sha256:
                    raise ProductionDriverError(
                        "SNAPSHOT_BINDING_MISMATCH", "MAI snapshot changed during startup"
                    )
                target_ready = self._system.attest_gpu_shared_capacity(
                    self._config.mai_gpu_index,
                    minimum_free_memory_mib=0,
                )
                if time.monotonic_ns() >= trusted_context.authority_deadline_monotonic_ns:
                    raise ProductionDriverError(
                        "OWNER_AUTHORITY_EXPIRED",
                        "handoff owner authority elapsed during MAI-ready GPU attestation",
                    )
                progress["target_ready_shared_gpu_attestation"] = cast(
                    JsonValue, production_shared_gpu_attestation_projection(target_ready)
                )
                _validate_shared_gpu_tenants(
                    target_ready,
                    baseline=self._shared_gpu_baseline,
                    owned_identity=target.identity,
                )
                evidence = ProductionModelHandoffEvidenceV1(
                    manifest_sha256=trusted_context.manifest_sha256,
                    runtime_config_sha256=production_runtime_config_sha256(self._config),
                    sequence_execution_scope=trusted_context.sequence_execution_scope,
                    sequence_scope_authority_sha256=(
                        trusted_context.sequence_scope_authority_sha256
                    ),
                    resource_topology=self._config.resource_topology,
                    gpu_lease_sha256=lease.lease_sha256,
                    source_host=PilotHostV1.QWEN3_VL,
                    target_host=PilotHostV1.MAI_UI,
                    source_stop=source_stop,
                    baseline_shared_gpu_attestation=self._shared_gpu_baseline,
                    post_stop_shared_gpu_attestation=post_stop,
                    target_command_sha256=production_command_spec_sha256(target.spec),
                    target_process=target.identity,
                    target_health_sha256=target_health,
                    target_snapshot_attestation_sha256=(target_snapshot_attestation_sha256),
                    target_ready_shared_gpu_attestation=target_ready,
                )
                self._handoff_evidence = evidence
                preimage = _production_model_handoff_evidence_preimage(evidence)
                return AdapterStageResultV1(
                    stage=RunStageV1.MAI_LIVE_SMOKE,
                    manifest_sha256=trusted_context.manifest_sha256,
                    evidence_sha256=production_model_handoff_evidence_sha256(evidence),
                    evidence_preimage=preimage,
                    actor_calls=0,
                    openai_calls=0,
                    actor_actions=0,
                    cost_usd_micros=0,
                    completed_units=("resource-handoff:QWEN3_VL:MAI_UI",),
                    provider_final_request_proven=False,
                )
            except Exception as exc:
                failure_code = _exception_code(exc, "RESOURCE_HANDOFF_FAILED")
                progress.update(
                    {
                        "failure_code": failure_code,
                        "gpu_lease_sha256": lease.lease_sha256,
                        "residual_capabilities": self._system.residual_capabilities(),
                        "status": "FAILED_CLEANUP_REQUIRED",
                    }
                )
                self._handoff_failure_evidence = canonical_json_bytes(
                    cast(
                        JsonValue,
                        {
                            "domain": "production-model-handoff-failure-evidence",
                            "schema_version": (PRODUCTION_MODEL_HANDOFF_EVIDENCE_SCHEMA_VERSION_V1),
                            "value": progress,
                        },
                    )
                )
                raise

    def _cleanup_owned(
        self,
    ) -> tuple[ProductionSharedGpuAttestationV1 | None, tuple[str, ...], str | None]:
        failure = False
        stopped_hosts: list[PilotHostV1] = []
        backend_container_id = None if self._backend is None else self._backend.container_id
        lease_sha256s = tuple(lease.lease_sha256 for lease in self._gpu_leases.values())
        for host, owned in reversed(tuple(self._models.items())):
            try:
                stop_evidence = self._system.stop_model(owned)
            except Exception:
                failure = True
            else:
                self._stop_evidence.append(stop_evidence)
                stopped_hosts.append(host)
        for host in stopped_hosts:
            self._models.pop(host, None)
            if (
                self._config.resource_topology
                is ProductionResourceTopologyV1.INDEPENDENT_GPU_CONCURRENT
            ):
                lease = self._gpu_leases.pop(host, None)
                if lease is not None:
                    lease.close()
        try:
            self._system.retry_pending_cleanup()
        except Exception:
            failure = True
        backend = self._backend
        if backend is not None:
            try:
                self._system.stop_backend(backend)
            except Exception:
                failure = True
            else:
                self._backend = None
        final_shared_attestation: ProductionSharedGpuAttestationV1 | None = None
        if (
            self._config.resource_topology
            is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
            and not self._models
            and self._shared_gpu_baseline is not None
            and self._gpu_leases
        ):
            try:
                final_shared_attestation = self._system.attest_gpu_shared_capacity(
                    self._config.qwen_gpu_index,
                    minimum_free_memory_mib=0,
                )
                _validate_shared_gpu_cleanup(
                    final_shared_attestation,
                    baseline=self._shared_gpu_baseline,
                    stopped_models=tuple(self._stop_evidence),
                )
                self._last_shared_cleanup_attestation = final_shared_attestation
            except Exception:
                failure = True
        if not failure and not self._models and self._backend is None:
            for lease in self._gpu_leases.values():
                lease.close()
            self._gpu_leases.clear()
            self._resources.clear()
            self._model_specs.clear()
        if failure:
            raise ProductionDriverError(
                "RESOURCE_CLEANUP_FAILED",
                "owned cleanup authority was retained for another attempt",
            )
        return final_shared_attestation, lease_sha256s, backend_container_id

    def cleanup(self, context: StageAdapterContextV1) -> None:
        trusted_context = _snapshot_context(context)
        if self._manifest_sha256 != trusted_context.manifest_sha256:
            raise ProductionDriverError("RESOURCE_BINDING_MISMATCH", "cleanup manifest differs")
        if self._evidence is not None and (
            self._evidence.sequence_execution_scope != trusted_context.sequence_execution_scope
            or self._evidence.sequence_scope_authority_sha256
            != trusted_context.sequence_scope_authority_sha256
        ):
            raise ProductionDriverError(
                "RESOURCE_BINDING_MISMATCH", "cleanup sequence scope differs"
            )
        if self._cleanup_success_evidence is not None:
            return
        try:
            current_cleanup_values = self._cleanup_owned()
            if self._reclaimed_cleanup_values is None:
                self._cache_reclaimed_cleanup(trusted_context, current_cleanup_values)
            cleanup_values = self._reclaimed_cleanup_values or current_cleanup_values
            final_shared_attestation, lease_sha256s, backend_container_id = cleanup_values
            final_shared_attestation = (
                final_shared_attestation or self._last_shared_cleanup_attestation
            )
        except Exception as exc:
            cleanup_failure_code = _exception_code(exc, "RESOURCE_CLEANUP_FAILED")
            self._failure_evidence = self._resource_failure_preimage(
                trusted_context,
                failure_code="RESOURCE_CLEANUP_FAILED",
                status="FAILED_CLEANUP_RETRY_REQUIRED",
                cleanup_status="RETRY_REQUIRED",
                cleanup_failure_code=cleanup_failure_code,
            )
            self._cleanup_failure_evidence = self._failure_evidence
            raise
        residual = self._system.residual_capabilities()
        projection: dict[str, JsonValue] = {
            "backend_container_id": backend_container_id,
            "gpu_lease_released": not self._gpu_leases,
            "gpu_lease_sha256s": list(lease_sha256s),
            "manifest_sha256": trusted_context.manifest_sha256,
            "resource_topology": self._config.resource_topology.value,
            "residual_capabilities": residual,
            "residual_capabilities_sha256": _hash_projection(
                "production-resource-residual-capabilities",
                cast(JsonValue, residual),
            ),
            "runtime_config_sha256": production_runtime_config_sha256(self._config),
            "reclaimed_cleanup_outcome": (
                None
                if self._reclaimed_cleanup_outcome is None
                else cast(JsonValue, json.loads(self._reclaimed_cleanup_outcome))
            ),
            "reclaimed_cleanup_outcome_sha256": (
                None
                if self._reclaimed_cleanup_outcome is None
                else hashlib.sha256(self._reclaimed_cleanup_outcome).hexdigest()
            ),
            "sequence_execution_scope": trusted_context.sequence_execution_scope,
            "sequence_scope_authority_sha256": (trusted_context.sequence_scope_authority_sha256),
            "status": "CLEANED",
            "stopped_models": [
                production_model_stop_evidence_projection(item) for item in self._stop_evidence
            ],
            "stopped_model_sha256s": [
                production_model_stop_evidence_sha256(item) for item in self._stop_evidence
            ],
        }
        if self._config.resource_topology is (
            ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        ):
            if self._shared_gpu_baseline is None:
                if (
                    self._evidence is not None
                    or self._models
                    or self._backend is not None
                    or self._gpu_leases
                    or any(cast(list[object], item) for item in residual.values())
                    or self._failure_evidence is None
                ):
                    raise ProductionDriverError(
                        "RESOURCE_CLEANUP_FAILED",
                        "pre-baseline shared cleanup state is not provably empty",
                    )
                projection.update(
                    {
                        "cleanup_outcome": "PRE_BASELINE_NO_MODEL_RECLAIMED",
                        "prepare_failure_evidence_sha256": hashlib.sha256(
                            self._failure_evidence
                        ).hexdigest(),
                    }
                )
            elif final_shared_attestation is None:
                raise ProductionDriverError(
                    "RESOURCE_CLEANUP_FAILED", "shared GPU cleanup evidence is incomplete"
                )
            else:
                projection.update(
                    {
                        "cleanup_outcome": "SHARED_MODELS_RECLAIMED",
                        "baseline_shared_gpu_attestation": (
                            production_shared_gpu_attestation_projection(self._shared_gpu_baseline)
                        ),
                        "final_shared_gpu_attestation": (
                            production_shared_gpu_attestation_projection(final_shared_attestation)
                        ),
                        "final_shared_gpu_attestation_sha256": (
                            production_shared_gpu_attestation_sha256(final_shared_attestation)
                        ),
                        "minimum_free_gpu_memory_mib": (self._config.minimum_free_gpu_memory_mib),
                        "vllm_gpu_memory_utilization": (self._config.vllm_gpu_memory_utilization),
                    }
                )
        self._cleanup_success_evidence = canonical_json_bytes(
            cast(
                JsonValue,
                {
                    "domain": "production-resource-cleanup-evidence",
                    "schema_version": (PRODUCTION_RESOURCE_CLEANUP_EVIDENCE_SCHEMA_VERSION_V1),
                    "value": projection,
                },
            )
        )

    def cleanup_success_evidence_preimage(self) -> bytes | None:
        return self._cleanup_success_evidence

    def handoff_failure_evidence_preimage(self) -> bytes | None:
        return self._handoff_failure_evidence

    def last_dispatch_evidence_preimage(self) -> bytes | None:
        return self._last_dispatch_evidence

    def last_dispatch_failure_evidence_preimage(self) -> bytes | None:
        return self._last_dispatch_failure_evidence

    def disable_host(self, host: PilotHostV1) -> str:
        """Permanently disable one host for this adapter lifetime."""

        if type(host) is not PilotHostV1:
            raise ProductionDriverError("UNTRUSTED_TYPE", "host must use exact enum")
        with self._lock:
            resource = self._resources.get(host)
            if resource is not None and not resource.independent_kill_switch:
                raise ProductionDriverError(
                    "HOST_KILL_UNAUTHORIZED", "manifest did not authorize an independent gate"
                )
            self._disabled_hosts.add(host)
            return _hash_projection(
                "production-host-disabled",
                cast(JsonValue, {"host": host.value, "status": "DISABLED"}),
            )

    def kill_host(self, host: PilotHostV1) -> str:
        """Disable and stop only one host model, preserving the peer and backend."""

        disabled_sha256 = self.disable_host(host)
        with self._lock:
            owned = self._models.get(host)
            if owned is None:
                return disabled_sha256
            try:
                stop_evidence = self._system.stop_model(owned)
            except Exception as exc:
                raise ProductionDriverError(
                    "HOST_KILL_CLEANUP_FAILED",
                    "host remains disabled and its process authority is retained",
                ) from exc
            self._stop_evidence.append(stop_evidence)
            self._models.pop(host, None)
            if (
                self._config.resource_topology
                is ProductionResourceTopologyV1.INDEPENDENT_GPU_CONCURRENT
            ):
                lease = self._gpu_leases.pop(host, None)
                if lease is not None:
                    lease.close()
            return _hash_projection(
                "production-host-killed",
                cast(
                    JsonValue,
                    {
                        "disable_sha256": disabled_sha256,
                        "host": host.value,
                        "model_pid": owned.identity.pid,
                        "status": "KILLED",
                    },
                ),
            )

    def require_dispatch(
        self,
        host: PilotHostV1,
        kind: ProductionDispatchKindV1,
        *,
        authority_deadline_monotonic_ns: int,
    ) -> str:
        """Re-attest mutable live identities immediately before one dispatch.

        The complete snapshot trees are hashed before model start and again
        after READY. Per-dispatch checks intentionally bind the owned PID,
        starttime, exact served-model registry, container ID/image/running
        state, and both loopback health endpoints without re-reading multi-GB
        immutable weights for every actor or GUI step.
        """

        if type(host) is not PilotHostV1 or type(kind) is not ProductionDispatchKindV1:
            raise ProductionDriverError("UNTRUSTED_TYPE", "dispatch gate type differs")
        if (
            type(authority_deadline_monotonic_ns) is not int
            or time.monotonic_ns() >= authority_deadline_monotonic_ns
        ):
            raise ProductionDriverError(
                "OWNER_AUTHORITY_EXPIRED", "dispatch owner authority elapsed"
            )
        with self._lock:
            if host in self._disabled_hosts:
                raise ProductionDriverError("HOST_DISABLED", "host dispatch gate is disabled")
            resource = self._resources.get(host)
            model = self._models.get(host)
            backend = self._backend
            if (
                self._evidence is None
                or resource is None
                or not resource.host_enabled
                or not resource.independent_kill_switch
                or model is None
                or backend is None
                or (
                    host not in self._gpu_leases
                    and not (
                        self._config.resource_topology
                        is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
                        and PilotHostV1.QWEN3_VL in self._gpu_leases
                    )
                )
            ):
                raise ProductionDriverError(
                    "RESOURCE_DISPATCH_UNAVAILABLE", "live resource ownership is incomplete"
                )
            self._last_dispatch_failure_evidence = None
            shared_gpu_attestation: ProductionSharedGpuAttestationV1 | None = None
            dispatch_projection: dict[str, JsonValue] = {
                "host": host.value,
                "kind": kind.value,
                "manifest_sha256": self._manifest_sha256,
                "resource_topology": self._config.resource_topology.value,
                "runtime_config_sha256": production_runtime_config_sha256(self._config),
                "sequence_execution_scope": self._evidence.sequence_execution_scope,
                "sequence_scope_authority_sha256": (self._evidence.sequence_scope_authority_sha256),
            }
            try:
                dispatch_projection["backend_attestation_sha256"] = self._system.attest_backend(
                    backend
                )
                dispatch_projection["model_attestation_sha256"] = self._system.attest_model(model)
                if (
                    self._config.resource_topology
                    is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
                ):
                    if self._shared_gpu_baseline is None:
                        raise ProductionDriverError(
                            "RESOURCE_DISPATCH_UNAVAILABLE", "shared GPU baseline is absent"
                        )
                    shared_gpu_attestation = self._system.attest_gpu_shared_capacity(
                        self._config.qwen_gpu_index,
                        minimum_free_memory_mib=0,
                    )
                    dispatch_projection["shared_gpu_attestation"] = cast(
                        JsonValue,
                        production_shared_gpu_attestation_projection(shared_gpu_attestation),
                    )
                    dispatch_projection["shared_gpu_attestation_sha256"] = (
                        production_shared_gpu_attestation_sha256(shared_gpu_attestation)
                    )
                    _validate_shared_gpu_tenants(
                        shared_gpu_attestation,
                        baseline=self._shared_gpu_baseline,
                        owned_identity=model.identity,
                    )
            except Exception as exc:
                if (
                    self._config.resource_topology
                    is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
                ):
                    dispatch_projection["failure_code"] = _exception_code(
                        exc, "RESOURCE_DISPATCH_FAILED"
                    )
                    dispatch_projection["status"] = "FAILED"
                    self._last_dispatch_failure_evidence = canonical_json_bytes(
                        cast(
                            JsonValue,
                            {
                                "domain": "production-resource-dispatch-evidence",
                                "schema_version": (
                                    PRODUCTION_SHARED_RESOURCE_EVIDENCE_SCHEMA_VERSION_V2
                                ),
                                "value": dispatch_projection,
                            },
                        )
                    )
                raise
            dispatch_projection["status"] = "PASSED"
            self._last_dispatch_evidence = canonical_json_bytes(
                cast(
                    JsonValue,
                    {
                        "domain": "production-resource-dispatch-evidence",
                        "schema_version": (
                            PRODUCTION_SHARED_RESOURCE_EVIDENCE_SCHEMA_VERSION_V2
                            if shared_gpu_attestation is not None
                            else PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION
                        ),
                        "value": dispatch_projection,
                    },
                )
            )
            assert self._last_dispatch_evidence is not None
            return hashlib.sha256(self._last_dispatch_evidence).hexdigest()

    @property
    def evidence(self) -> ProductionResourceStageEvidenceV1 | None:
        return self._evidence

    @property
    def handoff_evidence(self) -> ProductionModelHandoffEvidenceV1 | None:
        return self._handoff_evidence

    @property
    def resource_topology(self) -> ProductionResourceTopologyV1:
        return self._config.resource_topology

    @property
    def runtime_config_sha256(self) -> str:
        return production_runtime_config_sha256(self._config)

    @property
    def cleanup_upper_bound_seconds(self) -> int:
        projection = _production_resource_cleanup_bound_projection(self._config)
        return cast(int, projection["cleanup_upper_bound_seconds"])

    @property
    def cleanup_upper_bound_preimage(self) -> bytes:
        return _production_resource_cleanup_bound_preimage(self._config)

    @property
    def cleanup_upper_bound_sha256(self) -> str:
        return hashlib.sha256(self.cleanup_upper_bound_preimage).hexdigest()

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None:
        if stage is RunStageV1.RESOURCE_PREFLIGHT:
            return self._failure_evidence
        if stage is RunStageV1.MAI_LIVE_SMOKE:
            return self._cleanup_failure_evidence or self._handoff_failure_evidence
        return None

    @property
    def cpu_trace(self) -> CpuResourceLifecycleTraceV1:
        if type(self._system) is not _CpuRecordingResourceSystemV1:
            raise ProductionDriverError("CPU_TRACE_UNAVAILABLE", "production system has no trace")
        return self._system.trace


def build_cpu_test_resource_lifecycle_adapter_v1(
    config: ProductionRuntimeConfigV1,
    fault: CpuResourceLifecycleFaultV1 = CpuResourceLifecycleFaultV1.NONE,
) -> ProductionResourceLifecycleAdapterV1:
    if (
        type(config) is not ProductionRuntimeConfigV1
        or type(fault) is not CpuResourceLifecycleFaultV1
    ):
        raise ProductionDriverError("UNTRUSTED_TYPE", "runtime config type differs")
    return ProductionResourceLifecycleAdapterV1(
        config,
        system=_CpuRecordingResourceSystemV1(fault, seal=_MODULE_SEAL),
        seal=_MODULE_SEAL,
    )


def build_production_resource_lifecycle_adapter_v1(
    config: ProductionRuntimeConfigV1,
    *,
    confirmed_config_sha256: str,
) -> ProductionResourceLifecycleAdapterV1:
    if type(config) is not ProductionRuntimeConfigV1 or (
        confirmed_config_sha256 != production_runtime_config_sha256(config)
    ):
        raise ProductionDriverError(
            "RUNTIME_CONFIG_HASH_DRIFT", "confirmed runtime config hash differs"
        )
    return ProductionResourceLifecycleAdapterV1(
        config,
        system=_PosixProductionResourceSystemV1(config, seal=_MODULE_SEAL),
        seal=_MODULE_SEAL,
    )


@dataclass(frozen=True, slots=True)
class DriverCallCensusV1:
    """One exact actor-call census with independent rubric/policy roles."""

    actor_calls: int
    offline_rubric_evaluations: int
    rubric_openai_calls: int
    history_policy_openai_calls: int
    openai_calls: int
    actor_actions: int
    cost_usd_micros: int
    wall_time_ms: int

    def __post_init__(self) -> None:
        for name in (
            "actor_calls",
            "offline_rubric_evaluations",
            "rubric_openai_calls",
            "history_policy_openai_calls",
            "openai_calls",
            "actor_actions",
            "cost_usd_micros",
            "wall_time_ms",
        ):
            _require_nonnegative(getattr(self, name), name)
        if self.actor_calls != 1:
            raise ProductionDriverError("INVALID_CENSUS", "one decision must be one actor call")
        if self.offline_rubric_evaluations > 1:
            raise ProductionDriverError(
                "INVALID_CENSUS", "one actor call permits at most one offline rubric evaluation"
            )
        if (
            self.rubric_openai_calls > 2
            or self.history_policy_openai_calls > 1
            or (self.openai_calls != self.rubric_openai_calls + self.history_policy_openai_calls)
        ):
            raise ProductionDriverError(
                "INVALID_CENSUS",
                "one actor permits rubric generate/track and one history-policy call",
            )
        if self.actor_actions > 1:
            raise ProductionDriverError("INVALID_CENSUS", "one actor call emits at most one action")


@dataclass(frozen=True, slots=True)
class ActorDecisionEvidenceV1:
    """Hash-bound exact request/provider/action chain for one actor decision."""

    logical_call_id: str
    actor_call_index: int
    raw_request_sha256: str
    final_request_sha256: str
    provider_request_sha256: str
    provider_response_sha256: str
    exact_diff_sha256: str
    pre_provider_status: ProductionRuntimeAuditPreProviderStatusV1
    pre_provider_outcome: ProductionRuntimeAuditPreProviderOutcomeV1
    fallback_reason: SentinelFallbackReason | None
    fallback_check: str | None
    preflight_report_sha256: str
    case_execution_lease_sha256: str | None
    live_policy_factory_binding_sha256: str
    live_policy_authority_sha256: str | None
    rubric_attempt_receipt_sha256s: tuple[str, ...]
    history_policy_attempt_receipt_sha256: str | None
    actor_attempt_receipt_sha256: str
    sentinel_receipt_sha256: str
    provider_attempt_receipt_sha256: str
    runtime_audit_detail_sha256: str
    parser_result_sha256: str
    parsed_action_sha256: str
    executed_action_sha256: str | None
    census: DriverCallCensusV1

    def __post_init__(self) -> None:
        _require_safe_id(self.logical_call_id, "logical_call_id")
        if type(self.actor_call_index) is not int or self.actor_call_index < 1:
            raise ProductionDriverError(
                "INVALID_EVIDENCE", "actor_call_index must be a positive integer"
            )
        if (
            type(self.pre_provider_status) is not ProductionRuntimeAuditPreProviderStatusV1
            or type(self.pre_provider_outcome) is not ProductionRuntimeAuditPreProviderOutcomeV1
        ):
            raise ProductionDriverError(
                "INVALID_EVIDENCE", "pre-provider outcome/status type differs"
            )
        expected_outcomes = {
            ProductionRuntimeAuditPreProviderStatusV1.READY: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.READY}
            ),
            ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL: frozenset(
                {
                    ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL,
                    ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL,
                }
            ),
            ProductionRuntimeAuditPreProviderStatusV1.BYPASSED_ORIGINAL: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.BYPASSED_ORIGINAL}
            ),
            ProductionRuntimeAuditPreProviderStatusV1.OFF: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.OFF}
            ),
        }
        if self.pre_provider_outcome not in expected_outcomes[self.pre_provider_status]:
            raise ProductionDriverError("INVALID_EVIDENCE", "pre-provider outcome/status differ")
        if (
            self.fallback_reason is not None
            and type(self.fallback_reason) is not SentinelFallbackReason
        ):
            raise ProductionDriverError("INVALID_EVIDENCE", "fallback reason type differs")
        if self.fallback_check is not None:
            _require_safe_id(self.fallback_check, "fallback_check")
        if self.pre_provider_status in {
            ProductionRuntimeAuditPreProviderStatusV1.OFF,
            ProductionRuntimeAuditPreProviderStatusV1.READY,
        }:
            outcome_invalid = self.fallback_reason is not None or self.fallback_check is not None
        elif (
            self.pre_provider_status is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
        ):
            outcome_invalid = self.fallback_reason is None or self.fallback_check is None
        else:
            outcome_invalid = self.fallback_reason is not None or self.fallback_check is None
        if outcome_invalid:
            raise ProductionDriverError(
                "INVALID_EVIDENCE", "pre-provider fallback classification differs"
            )
        for name in (
            "raw_request_sha256",
            "final_request_sha256",
            "provider_request_sha256",
            "provider_response_sha256",
            "exact_diff_sha256",
            "preflight_report_sha256",
            "live_policy_factory_binding_sha256",
            "actor_attempt_receipt_sha256",
            "sentinel_receipt_sha256",
            "provider_attempt_receipt_sha256",
            "runtime_audit_detail_sha256",
            "parser_result_sha256",
            "parsed_action_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if self.case_execution_lease_sha256 is not None:
            _require_sha256(self.case_execution_lease_sha256, "case_execution_lease_sha256")
        if self.executed_action_sha256 is not None:
            _require_sha256(self.executed_action_sha256, "executed_action_sha256")
        if self.live_policy_authority_sha256 is not None:
            _require_sha256(self.live_policy_authority_sha256, "live_policy_authority_sha256")
        if self.history_policy_attempt_receipt_sha256 is not None:
            _require_sha256(
                self.history_policy_attempt_receipt_sha256,
                "history_policy_attempt_receipt_sha256",
            )
        raw_rubric_receipts: object = self.rubric_attempt_receipt_sha256s
        if (
            type(raw_rubric_receipts) is not tuple
            or len(raw_rubric_receipts) > 2
            or any(
                type(item) is not str or _SHA256.fullmatch(item) is None
                for item in cast(tuple[object, ...], raw_rubric_receipts)
            )
        ):
            raise ProductionDriverError("INVALID_EVIDENCE", "rubric attempt receipts are not exact")
        if type(self.census) is not DriverCallCensusV1:
            raise ProductionDriverError("INVALID_CENSUS", "decision census type differs")
        if self.provider_request_sha256 != self.final_request_sha256:
            raise ProductionDriverError(
                "PROVIDER_FINAL_REQUEST_MISMATCH", "provider did not receive the final request"
            )
        semantic_receipts_present = bool(self.rubric_attempt_receipt_sha256s) or (
            self.history_policy_attempt_receipt_sha256 is not None
        )
        complete_semantic_authority = (
            self.live_policy_authority_sha256 is not None
            and self.case_execution_lease_sha256 is not None
        )
        if semantic_receipts_present != complete_semantic_authority:
            raise ProductionDriverError(
                "LIVE_ATTEMPT_CENSUS_MISMATCH",
                "semantic terminal receipts differ from their lease/authority",
            )
        if self.census.rubric_openai_calls > len(self.rubric_attempt_receipt_sha256s):
            raise ProductionDriverError(
                "LIVE_ATTEMPT_CENSUS_MISMATCH",
                "rubric dispatches exceed terminal attempt receipts",
            )
        history_receipt_count = int(self.history_policy_attempt_receipt_sha256 is not None)
        if self.census.history_policy_openai_calls > history_receipt_count:
            raise ProductionDriverError(
                "LIVE_ATTEMPT_CENSUS_MISMATCH",
                "history dispatches exceed terminal attempt receipts",
            )
        if self.census.actor_actions == 0 and self.executed_action_sha256 is not None:
            raise ProductionDriverError("ACTION_CENSUS_MISMATCH", "unexecuted action has a hash")
        if self.census.actor_actions == 1 and (
            self.executed_action_sha256 != self.parsed_action_sha256
        ):
            raise ProductionDriverError(
                "ACTION_CENSUS_MISMATCH", "executed action differs from the parsed action"
            )


def _semantic_pre_provider_outcome_admitted(value: ActorDecisionEvidenceV1) -> bool:
    if value.pre_provider_outcome is ProductionRuntimeAuditPreProviderOutcomeV1.READY:
        return value.pre_provider_status is ProductionRuntimeAuditPreProviderStatusV1.READY
    if (
        value.pre_provider_outcome
        is not ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL
    ):
        return False
    return (
        value.pre_provider_status is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
        and value.fallback_reason is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
        and value.fallback_check == "r2_4_no_history_r21_v1_compatibility"
        and value.census.rubric_openai_calls == 2
        and value.census.history_policy_openai_calls == 0
        and value.census.openai_calls == 2
        and len(value.rubric_attempt_receipt_sha256s) == 2
        and value.history_policy_attempt_receipt_sha256 is None
    )


@dataclass(frozen=True, slots=True)
class DriverStageCensusV1:
    actor_calls: int
    offline_rubric_evaluations: int
    rubric_openai_calls: int
    history_policy_openai_calls: int
    openai_calls: int
    actor_actions: int
    cost_usd_micros: int
    wall_time_ms: int

    def __post_init__(self) -> None:
        for name in (
            "actor_calls",
            "offline_rubric_evaluations",
            "rubric_openai_calls",
            "history_policy_openai_calls",
            "openai_calls",
            "actor_actions",
            "cost_usd_micros",
            "wall_time_ms",
        ):
            _require_nonnegative(getattr(self, name), name)
        if self.openai_calls != (self.rubric_openai_calls + self.history_policy_openai_calls):
            raise ProductionDriverError("INVALID_CENSUS", "stage OpenAI role census differs")
        if self.actor_actions > self.actor_calls:
            raise ProductionDriverError("INVALID_CENSUS", "actions exceed actor decisions")


@dataclass(frozen=True, slots=True)
class _ValidatedUnitJournalReferenceV1:
    """Module-sealed proof that one compact journal reference was read back."""

    expected_unit_id: str
    reference_preimage_sha256: str
    reference_sha256: str
    blob_sha256: str
    blob_byte_count: int
    blob_safe_locator: str
    owner_audit_root_identity_sha256: str
    resource_dispatch_records_sha256: str
    resource_dispatch_record_count: int
    _resolver: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _MODULE_SEAL:
            raise ProductionDriverError(
                "INVALID_UNIT_JOURNAL", "unit journal validation seal differs"
            )
        _require_safe_id(self.expected_unit_id, "unit_journal.expected_unit_id")
        for name in (
            "reference_preimage_sha256",
            "reference_sha256",
            "blob_sha256",
            "owner_audit_root_identity_sha256",
            "resource_dispatch_records_sha256",
        ):
            _require_sha256(getattr(self, name), f"unit_journal.{name}")
        if (
            type(self.blob_byte_count) is not int
            or self.blob_byte_count <= _MAX_INLINE_UNIT_EVIDENCE_JOURNAL_BYTES
            or type(self.blob_safe_locator) is not str
            or self.blob_safe_locator != f"{self.blob_sha256}{_UNIT_EVIDENCE_BLOB_SUFFIX}"
            or type(self.resource_dispatch_record_count) is not int
            or self.resource_dispatch_record_count < 0
        ):
            raise ProductionDriverError(
                "INVALID_UNIT_JOURNAL", "validated unit journal reference differs"
            )

    def revalidate(self, reference_raw: bytes, *, expected_unit_id: str) -> None:
        resolver = self._resolver
        if type(resolver) is not _ProductionFixedExecutionPortV1:
            raise ProductionDriverError("INVALID_UNIT_JOURNAL", "unit journal resolver differs")
        current = resolver._validated_smoke_unit_journal_reference(
            reference_raw, expected_unit_id=expected_unit_id
        )
        if current is None or _validated_unit_journal_reference_projection(
            current
        ) != _validated_unit_journal_reference_projection(self):
            raise ProductionDriverError(
                "INVALID_UNIT_JOURNAL", "unit journal validation binding differs"
            )


def _validated_unit_journal_reference_projection(
    value: _ValidatedUnitJournalReferenceV1,
) -> dict[str, JsonValue]:
    if type(value) is not _ValidatedUnitJournalReferenceV1:
        raise ProductionDriverError("INVALID_UNIT_JOURNAL", "unit journal validation type differs")
    return {
        "blob": {
            "algorithm": "sha256",
            "byte_count": value.blob_byte_count,
            "safe_locator": value.blob_safe_locator,
            "sha256": value.blob_sha256,
        },
        "expected_unit_id": value.expected_unit_id,
        "owner_audit_root_identity_sha256": value.owner_audit_root_identity_sha256,
        "reference_preimage_sha256": value.reference_preimage_sha256,
        "reference_sha256": value.reference_sha256,
        "resource_dispatch_record_count": value.resource_dispatch_record_count,
        "resource_dispatch_records_sha256": value.resource_dispatch_records_sha256,
        "schema_version": _VALIDATED_UNIT_EVIDENCE_BLOB_REFERENCE_SCHEMA_VERSION,
        "validation_status": "EXACT_OWNER_ONLY_READBACK",
    }


@dataclass(frozen=True, slots=True)
class SmokeCaseEvidenceV1:
    manifest_sha256: str
    run_id: str
    stage: RunStageV1
    host: PilotHostV1
    sequence_index: int
    case_id: str
    task_id: str
    mode: SmokeModeV1
    actor_resource_sha256: str
    history_policy_stage_sha256: str
    request_fixture_sha256: str
    request_fixture_byte_count: int
    decision: ActorDecisionEvidenceV1
    cleanup_receipt_sha256: str
    unit_journal_preimage: bytes | None
    unit_journal_sha256: str | None
    unit_journal_validated_reference: _ValidatedUnitJournalReferenceV1 | None
    census: DriverStageCensusV1

    def __post_init__(self) -> None:
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        _require_safe_id(self.run_id, "run_id")
        if type(self.stage) is not RunStageV1 or type(self.host) is not PilotHostV1:
            raise ProductionDriverError("INVALID_EVIDENCE", "smoke stage/host enum differs")
        if type(self.sequence_index) is not int or not 0 <= self.sequence_index < 3:
            raise ProductionDriverError("INVALID_EVIDENCE", "smoke sequence index differs")
        _require_safe_id(self.case_id, "case_id")
        _require_safe_id(self.task_id, "task_id")
        if type(self.mode) is not SmokeModeV1:
            raise ProductionDriverError("INVALID_EVIDENCE", "smoke mode differs")
        for name in (
            "actor_resource_sha256",
            "history_policy_stage_sha256",
            "request_fixture_sha256",
            "cleanup_receipt_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if type(self.request_fixture_byte_count) is not int or self.request_fixture_byte_count < 1:
            raise ProductionDriverError("INVALID_EVIDENCE", "fixture byte count differs")
        if (
            type(self.decision) is not ActorDecisionEvidenceV1
            or type(self.census) is not DriverStageCensusV1
        ):
            raise ProductionDriverError("INVALID_EVIDENCE", "smoke evidence type differs")
        if (self.unit_journal_preimage is None) != (self.unit_journal_sha256 is None):
            raise ProductionDriverError(
                "INVALID_UNIT_JOURNAL", "smoke unit journal presence differs"
            )
        if self.unit_journal_preimage is None:
            if self.unit_journal_validated_reference is not None:
                raise ProductionDriverError(
                    "INVALID_UNIT_JOURNAL", "orphan unit journal validation differs"
                )
            return
        if self.unit_journal_preimage is not None:
            assert self.unit_journal_sha256 is not None
            if type(self.unit_journal_preimage) is not bytes:
                raise ProductionDriverError(
                    "INVALID_UNIT_JOURNAL", "smoke unit journal must use exact bytes"
                )
            try:
                journal = json.loads(self.unit_journal_preimage)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProductionDriverError(
                    "INVALID_UNIT_JOURNAL", "smoke unit journal is not JSON"
                ) from exc
            if (
                hashlib.sha256(self.unit_journal_preimage).hexdigest() != self.unit_journal_sha256
                or canonical_json_bytes(cast(JsonValue, journal)) != self.unit_journal_preimage
                or type(journal) is not dict
            ):
                raise ProductionDriverError(
                    "INVALID_UNIT_JOURNAL", "smoke unit journal binding differs"
                )
            expected_unit_id = f"smoke:{self.host.value}:{self.mode.value}"
            is_blob_reference = (
                journal.get("schema_version") == _UNIT_EVIDENCE_BLOB_REFERENCE_SCHEMA_VERSION
                or journal.get("storage") == _UNIT_EVIDENCE_BLOB_STORAGE_KIND
            )
            if is_blob_reference:
                validation = self.unit_journal_validated_reference
                if type(validation) is not _ValidatedUnitJournalReferenceV1:
                    raise ProductionDriverError(
                        "INVALID_UNIT_JOURNAL",
                        "smoke unit journal blob lacks validated readback",
                    )
                validation.revalidate(self.unit_journal_preimage, expected_unit_id=expected_unit_id)
                if validation.resource_dispatch_record_count < 1:
                    raise ProductionDriverError(
                        "INVALID_UNIT_JOURNAL",
                        "smoke unit journal lacks its dispatch census",
                    )
            else:
                if self.unit_journal_validated_reference is not None:
                    raise ProductionDriverError(
                        "INVALID_UNIT_JOURNAL",
                        "inline smoke unit journal has an orphan blob validation",
                    )
                dispatches = journal.get("resource_dispatch_records")
                if (
                    journal.get("unit_id") != expected_unit_id
                    or type(dispatches) is not list
                    or not dispatches
                    or journal.get("resource_dispatch_records_sha256")
                    != _hash_projection(
                        "production-unit-resource-dispatch-journal",
                        cast(JsonValue, dispatches),
                    )
                ):
                    raise ProductionDriverError(
                        "INVALID_UNIT_JOURNAL",
                        "smoke unit journal lacks its dispatch census",
                    )


@dataclass(frozen=True, slots=True)
class OfficialTaskResultEvidenceV1:
    task_id: str
    evaluator_id: str
    score_ppm: int
    successful: bool
    result_payload_sha256: str
    reason_sha256: str

    def __post_init__(self) -> None:
        _require_safe_id(self.task_id, "official_result.task_id")
        if self.evaluator_id != OFFICIAL_RESULT_EVALUATOR_ID_V1:
            raise ProductionDriverError(
                "INVALID_OFFICIAL_RESULT", "official evaluator identity differs"
            )
        if type(self.score_ppm) is not int or not 0 <= self.score_ppm <= 1_000_000:
            raise ProductionDriverError("INVALID_OFFICIAL_RESULT", "score is outside [0, 1]")
        if type(self.successful) is not bool:
            raise ProductionDriverError("INVALID_OFFICIAL_RESULT", "success flag is not bool")
        _require_sha256(self.result_payload_sha256, "result_payload_sha256")
        _require_sha256(self.reason_sha256, "reason_sha256")


@dataclass(frozen=True, slots=True)
class PilotCellEvidenceV1:
    manifest_sha256: str
    run_id: str
    sequence_index: int
    task_id: str
    task_parameters_sha256: str
    reset_seed: int
    host: PilotHostV1
    arm: PilotArmV1
    sentinel_mode: str
    actor_resource_sha256: str
    history_policy_stage_sha256: str
    reset_receipt_sha256: str
    effective_reset_state_sha256: str
    decisions: tuple[ActorDecisionEvidenceV1, ...]
    official_result: OfficialTaskResultEvidenceV1
    cleanup_receipt_sha256: str
    census: DriverStageCensusV1

    def __post_init__(self) -> None:
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        _require_safe_id(self.run_id, "run_id")
        if type(self.sequence_index) is not int or not 0 <= self.sequence_index < 120:
            raise ProductionDriverError("INVALID_EVIDENCE", "pilot sequence index differs")
        _require_safe_id(self.task_id, "task_id")
        _require_sha256(self.task_parameters_sha256, "task_parameters_sha256")
        if type(self.reset_seed) is not int or not 0 <= self.reset_seed <= 2_147_483_647:
            raise ProductionDriverError("INVALID_EVIDENCE", "reset seed differs")
        if type(self.host) is not PilotHostV1 or type(self.arm) is not PilotArmV1:
            raise ProductionDriverError("INVALID_EVIDENCE", "pilot host/arm enum differs")
        expected_mode = "OFF" if self.arm is PilotArmV1.BASELINE else "ACTIVE"
        if self.sentinel_mode != expected_mode:
            raise ProductionDriverError("INVALID_EVIDENCE", "pilot arm/mode differs")
        for name in (
            "actor_resource_sha256",
            "history_policy_stage_sha256",
            "reset_receipt_sha256",
            "effective_reset_state_sha256",
            "cleanup_receipt_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if (
            type(self.decisions) is not tuple
            or not self.decisions
            or any(type(item) is not ActorDecisionEvidenceV1 for item in self.decisions)
        ):
            raise ProductionDriverError("INVALID_EVIDENCE", "pilot decisions differ")
        if type(self.official_result) is not OfficialTaskResultEvidenceV1 or (
            type(self.census) is not DriverStageCensusV1
        ):
            raise ProductionDriverError("INVALID_EVIDENCE", "pilot result/census type differs")


@dataclass(frozen=True, slots=True)
class SmokeStageEvidenceV1:
    manifest_sha256: str
    run_id: str
    stage: RunStageV1
    host: PilotHostV1
    actor_resource_sha256: str
    history_policy_stage_sha256: str
    cases: tuple[SmokeCaseEvidenceV1, ...]
    census: DriverStageCensusV1
    schema_version: str = PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in {
            PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
            PRODUCTION_SHARED_SMOKE_EVIDENCE_SCHEMA_VERSION_V2,
        }:
            raise ProductionDriverError("UNKNOWN_SCHEMA", "smoke evidence schema differs")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        _require_safe_id(self.run_id, "run_id")
        if type(self.stage) is not RunStageV1 or type(self.host) is not PilotHostV1:
            raise ProductionDriverError("INVALID_EVIDENCE", "smoke stage identity differs")
        _require_sha256(self.actor_resource_sha256, "actor_resource_sha256")
        _require_sha256(self.history_policy_stage_sha256, "history_policy_stage_sha256")
        if (
            type(self.cases) is not tuple
            or len(self.cases) != 3
            or any(type(item) is not SmokeCaseEvidenceV1 for item in self.cases)
        ):
            raise ProductionDriverError("INVALID_EVIDENCE", "smoke needs three exact cases")
        if type(self.census) is not DriverStageCensusV1:
            raise ProductionDriverError("INVALID_CENSUS", "smoke stage census type differs")
        journals_present = tuple(item.unit_journal_preimage is not None for item in self.cases)
        if (
            self.schema_version == PRODUCTION_SHARED_SMOKE_EVIDENCE_SCHEMA_VERSION_V2
            and not all(journals_present)
        ) or (
            self.schema_version == PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION
            and any(journals_present)
        ):
            raise ProductionDriverError(
                "INVALID_UNIT_JOURNAL", "smoke stage schema/journal variant differs"
            )


@dataclass(frozen=True, slots=True)
class PilotStageEvidenceV1:
    manifest_sha256: str
    run_id: str
    pilot_manifest_sha256: str
    actor_resources_sha256: str
    history_policy_stage_sha256: str
    cells: tuple[PilotCellEvidenceV1, ...]
    census: DriverStageCensusV1
    schema_version: str = PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION:
            raise ProductionDriverError("UNKNOWN_SCHEMA", "pilot evidence schema differs")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        _require_safe_id(self.run_id, "run_id")
        for name in (
            "pilot_manifest_sha256",
            "actor_resources_sha256",
            "history_policy_stage_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if (
            type(self.cells) is not tuple
            or not 80 <= len(self.cells) <= 120
            or len(self.cells) % 4 != 0
            or any(type(item) is not PilotCellEvidenceV1 for item in self.cells)
        ):
            raise ProductionDriverError(
                "INVALID_EVIDENCE", "pilot needs 80--120 cells in exact four-arm groups"
            )
        if type(self.census) is not DriverStageCensusV1:
            raise ProductionDriverError("INVALID_CENSUS", "pilot stage census type differs")


def _census_projection(value: DriverStageCensusV1 | DriverCallCensusV1) -> dict[str, JsonValue]:
    return {
        "actor_actions": value.actor_actions,
        "actor_calls": value.actor_calls,
        "cost_usd_micros": value.cost_usd_micros,
        "history_policy_openai_calls": value.history_policy_openai_calls,
        "offline_rubric_evaluations": value.offline_rubric_evaluations,
        "openai_calls": value.openai_calls,
        "rubric_openai_calls": value.rubric_openai_calls,
        "wall_time_ms": value.wall_time_ms,
    }


def _decision_projection(value: ActorDecisionEvidenceV1) -> dict[str, JsonValue]:
    return {
        "actor_call_index": value.actor_call_index,
        "census": cast(JsonValue, _census_projection(value.census)),
        "exact_diff_sha256": value.exact_diff_sha256,
        "pre_provider_status": value.pre_provider_status.value,
        "pre_provider_outcome": value.pre_provider_outcome.value,
        "fallback_reason": (None if value.fallback_reason is None else value.fallback_reason.value),
        "fallback_check": value.fallback_check,
        "actor_attempt_receipt_sha256": value.actor_attempt_receipt_sha256,
        "case_execution_lease_sha256": value.case_execution_lease_sha256,
        "executed_action_sha256": value.executed_action_sha256,
        "final_request_sha256": value.final_request_sha256,
        "history_policy_attempt_receipt_sha256": value.history_policy_attempt_receipt_sha256,
        "live_policy_authority_sha256": value.live_policy_authority_sha256,
        "live_policy_factory_binding_sha256": value.live_policy_factory_binding_sha256,
        "logical_call_id": value.logical_call_id,
        "parsed_action_sha256": value.parsed_action_sha256,
        "parser_result_sha256": value.parser_result_sha256,
        "provider_attempt_receipt_sha256": value.provider_attempt_receipt_sha256,
        "provider_request_sha256": value.provider_request_sha256,
        "provider_response_sha256": value.provider_response_sha256,
        "preflight_report_sha256": value.preflight_report_sha256,
        "raw_request_sha256": value.raw_request_sha256,
        "rubric_attempt_receipt_sha256s": list(value.rubric_attempt_receipt_sha256s),
        "runtime_audit_detail_sha256": value.runtime_audit_detail_sha256,
        "sentinel_receipt_sha256": value.sentinel_receipt_sha256,
    }


def _smoke_case_evidence_projection(value: SmokeCaseEvidenceV1) -> dict[str, JsonValue]:
    projection: dict[str, JsonValue] = {
        "actor_resource_sha256": value.actor_resource_sha256,
        "case_id": value.case_id,
        "census": _census_projection(value.census),
        "cleanup_receipt_sha256": value.cleanup_receipt_sha256,
        "decision": _decision_projection(value.decision),
        "history_policy_stage_sha256": value.history_policy_stage_sha256,
        "host": value.host.value,
        "manifest_sha256": value.manifest_sha256,
        "mode": value.mode.value,
        "request_fixture_byte_count": value.request_fixture_byte_count,
        "request_fixture_sha256": value.request_fixture_sha256,
        "run_id": value.run_id,
        "sequence_index": value.sequence_index,
        "stage": value.stage.value,
        "task_id": value.task_id,
    }
    if value.unit_journal_preimage is not None:
        assert value.unit_journal_sha256 is not None
        validated_reference = value.unit_journal_validated_reference
        validation_projection: dict[str, JsonValue] | None = None
        if validated_reference is not None:
            validated_reference.revalidate(
                value.unit_journal_preimage,
                expected_unit_id=f"smoke:{value.host.value}:{value.mode.value}",
            )
            validation_projection = _validated_unit_journal_reference_projection(
                validated_reference
            )
        projection.update(
            {
                "unit_journal": cast(JsonValue, json.loads(value.unit_journal_preimage)),
                "unit_journal_byte_count": len(value.unit_journal_preimage),
                "unit_journal_sha256": value.unit_journal_sha256,
            }
        )
        if validation_projection is not None:
            projection["unit_journal_validated_reference"] = cast(JsonValue, validation_projection)
    return projection


def _pilot_cell_evidence_projection(value: PilotCellEvidenceV1) -> dict[str, JsonValue]:
    official = value.official_result
    return {
        "actor_resource_sha256": value.actor_resource_sha256,
        "arm": value.arm.value,
        "census": _census_projection(value.census),
        "cleanup_receipt_sha256": value.cleanup_receipt_sha256,
        "decisions": [_decision_projection(call) for call in value.decisions],
        "effective_reset_state_sha256": value.effective_reset_state_sha256,
        "history_policy_stage_sha256": value.history_policy_stage_sha256,
        "host": value.host.value,
        "manifest_sha256": value.manifest_sha256,
        "official_result": {
            "evaluator_id": official.evaluator_id,
            "reason_sha256": official.reason_sha256,
            "result_payload_sha256": official.result_payload_sha256,
            "score_ppm": official.score_ppm,
            "successful": official.successful,
            "task_id": official.task_id,
        },
        "reset_receipt_sha256": value.reset_receipt_sha256,
        "reset_seed": value.reset_seed,
        "run_id": value.run_id,
        "sentinel_mode": value.sentinel_mode,
        "sequence_index": value.sequence_index,
        "task_id": value.task_id,
        "task_parameters_sha256": value.task_parameters_sha256,
    }


def smoke_stage_evidence_projection(value: SmokeStageEvidenceV1) -> dict[str, JsonValue]:
    if type(value) is not SmokeStageEvidenceV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "smoke evidence must use exact type")
    cases = [cast(JsonValue, _smoke_case_evidence_projection(item)) for item in value.cases]
    return {
        "actor_resource_sha256": value.actor_resource_sha256,
        "cases": cases,
        "census": cast(JsonValue, _census_projection(value.census)),
        "history_policy_stage_sha256": value.history_policy_stage_sha256,
        "host": value.host.value,
        "manifest_sha256": value.manifest_sha256,
        "run_id": value.run_id,
        "schema_version": value.schema_version,
        "stage": value.stage.value,
    }


def pilot_stage_evidence_projection(value: PilotStageEvidenceV1) -> dict[str, JsonValue]:
    if type(value) is not PilotStageEvidenceV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "pilot evidence must use exact type")
    cells = [cast(JsonValue, _pilot_cell_evidence_projection(item)) for item in value.cells]
    return {
        "actor_resources_sha256": value.actor_resources_sha256,
        "cells": cells,
        "census": cast(JsonValue, _census_projection(value.census)),
        "history_policy_stage_sha256": value.history_policy_stage_sha256,
        "manifest_sha256": value.manifest_sha256,
        "pilot_manifest_sha256": value.pilot_manifest_sha256,
        "run_id": value.run_id,
        "schema_version": value.schema_version,
    }


def smoke_stage_evidence_sha256(value: SmokeStageEvidenceV1) -> str:
    return canonical_sha256(cast(JsonValue, smoke_stage_evidence_projection(value)))


def pilot_stage_evidence_sha256(value: PilotStageEvidenceV1) -> str:
    return canonical_sha256(cast(JsonValue, pilot_stage_evidence_projection(value)))


def _sum_census(
    values: tuple[DriverCallCensusV1 | DriverStageCensusV1, ...],
) -> DriverStageCensusV1:
    return DriverStageCensusV1(
        actor_calls=sum(value.actor_calls for value in values),
        offline_rubric_evaluations=sum(value.offline_rubric_evaluations for value in values),
        rubric_openai_calls=sum(value.rubric_openai_calls for value in values),
        history_policy_openai_calls=sum(value.history_policy_openai_calls for value in values),
        openai_calls=sum(value.openai_calls for value in values),
        actor_actions=sum(value.actor_actions for value in values),
        cost_usd_micros=sum(value.cost_usd_micros for value in values),
        wall_time_ms=sum(value.wall_time_ms for value in values),
    )


def _snapshot_context(value: StageAdapterContextV1) -> StageAdapterContextV1:
    if type(value) is not StageAdapterContextV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "adapter context must use exact type")
    _require_sha256(value.manifest_sha256, "context.manifest_sha256")
    _require_sha256(
        value.sequence_scope_authority_sha256,
        "context.sequence_scope_authority_sha256",
    )
    if value.sequence_execution_scope not in {"R24_R25_FULL", "R24_LIVE_SMOKE_ONLY"}:
        raise ProductionDriverError("INVALID_CONTEXT", "sequence execution scope differs")
    _require_safe_id(value.run_id, "context.run_id")
    if (
        type(value.source_commit) is not str
        or re.fullmatch(r"[0-9a-f]{40}", value.source_commit) is None
    ):
        raise ProductionDriverError("INVALID_CONTEXT", "source commit differs")
    for name in (
        "remaining_actor_calls",
        "remaining_openai_calls",
        "remaining_cost_usd_micros",
        "remaining_wall_time_ms",
    ):
        _require_nonnegative(getattr(value, name), f"context.{name}")
    if (
        type(value.authority_deadline_monotonic_ns) is not int
        or value.authority_deadline_monotonic_ns <= 0
    ):
        raise ProductionDriverError("INVALID_CONTEXT", "authority deadline differs")
    if time.monotonic_ns() >= value.authority_deadline_monotonic_ns:
        raise ProductionDriverError("OWNER_AUTHORITY_EXPIRED", "stage authority elapsed")
    return StageAdapterContextV1(
        manifest_sha256=value.manifest_sha256,
        sequence_execution_scope=value.sequence_execution_scope,
        sequence_scope_authority_sha256=value.sequence_scope_authority_sha256,
        run_id=value.run_id,
        source_commit=value.source_commit,
        remaining_actor_calls=value.remaining_actor_calls,
        remaining_openai_calls=value.remaining_openai_calls,
        remaining_cost_usd_micros=value.remaining_cost_usd_micros,
        remaining_wall_time_ms=value.remaining_wall_time_ms,
        authority_deadline_monotonic_ns=value.authority_deadline_monotonic_ns,
    )


def _snapshot_openai_stage(value: OpenAIResponsesStageV1) -> OpenAIResponsesStageV1:
    if type(value) is not OpenAIResponsesStageV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "OpenAI stage must use exact type")
    return OpenAIResponsesStageV1(
        role=value.role,
        model=value.model,
        endpoint=value.endpoint,
        transport_kind=value.transport_kind,
        transport_authority=value.transport_authority,
        openai_sdk_version=value.openai_sdk_version,
        sdk_max_retries=value.sdk_max_retries,
        external_network_on_call=value.external_network_on_call,
        model_on_call=value.model_on_call,
        max_output_tokens=value.max_output_tokens,
        timeout_ms=value.timeout_ms,
        max_attempts=value.max_attempts,
        store=value.store,
    )


def _history_policy_stage(
    values: tuple[OpenAIResponsesStageV1, ...],
) -> tuple[OpenAIResponsesStageV1, str]:
    if type(values) is not tuple:
        raise ProductionDriverError("UNTRUSTED_TYPE", "OpenAI stages must use an exact tuple")
    trusted = tuple(_snapshot_openai_stage(value) for value in values)
    policies = tuple(value for value in trusted if value.role is OpenAIRoleV1.HISTORY_POLICY)
    rubrics = tuple(value for value in trusted if value.role is OpenAIRoleV1.RUBRIC)
    if len(policies) != 1 or len(rubrics) > 1 or len(trusted) != len(policies) + len(rubrics):
        raise ProductionDriverError(
            "INVALID_LIVE_OPENAI_MATRIX",
            "one history-policy and at most one independent rubric stage are permitted",
        )
    stage = policies[0]
    projection: JsonValue = {
        "endpoint": stage.endpoint,
        "external_network_on_call": stage.external_network_on_call,
        "max_attempts": stage.max_attempts,
        "max_output_tokens": stage.max_output_tokens,
        "model": stage.model,
        "model_on_call": stage.model_on_call,
        "openai_sdk_version": stage.openai_sdk_version,
        "role": stage.role.value,
        "sdk_max_retries": stage.sdk_max_retries,
        "store": stage.store,
        "timeout_ms": stage.timeout_ms,
        "transport_authority": stage.transport_authority,
        "transport_kind": stage.transport_kind,
    }
    return stage, _hash_projection("history-policy-stage", projection)


def _snapshot_resource(value: SnapshotResourceV1) -> SnapshotResourceV1:
    if type(value) is not SnapshotResourceV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "resource must use exact type")
    return SnapshotResourceV1(
        host=value.host,
        history_codec_id=value.history_codec_id,
        snapshot_path=value.snapshot_path,
        snapshot_storage_root=value.snapshot_storage_root,
        snapshot_tree_algorithm=value.snapshot_tree_algorithm,
        snapshot_tree_sha256=value.snapshot_tree_sha256,
        snapshot_total_bytes=value.snapshot_total_bytes,
        snapshot_file_count=value.snapshot_file_count,
        actor_endpoint=value.actor_endpoint,
        served_model_id=value.served_model_id,
        host_enabled=value.host_enabled,
        independent_kill_switch=value.independent_kill_switch,
    )


def _resource_projection(value: SnapshotResourceV1) -> dict[str, JsonValue]:
    return {
        "actor_endpoint": value.actor_endpoint,
        "history_codec_id": value.history_codec_id,
        "host": value.host.value,
        "host_enabled": value.host_enabled,
        "independent_kill_switch": value.independent_kill_switch,
        "served_model_id": value.served_model_id,
        "snapshot_file_count": value.snapshot_file_count,
        "snapshot_path": value.snapshot_path,
        "snapshot_storage_root": value.snapshot_storage_root,
        "snapshot_total_bytes": value.snapshot_total_bytes,
        "snapshot_tree_algorithm": value.snapshot_tree_algorithm,
        "snapshot_tree_sha256": value.snapshot_tree_sha256,
    }


def _resource_sha256(value: SnapshotResourceV1) -> str:
    return _hash_projection("actor-resource", cast(JsonValue, _resource_projection(value)))


def _snapshot_plan(value: HostLiveSmokePlanV1) -> HostLiveSmokePlanV1:
    if type(value) is not HostLiveSmokePlanV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "smoke plan must use exact type")
    cases: list[LiveSmokeCaseV1] = []
    for item in value.cases:
        if type(item) is not LiveSmokeCaseV1:
            raise ProductionDriverError("UNTRUSTED_TYPE", "smoke case must use exact type")
        cases.append(
            LiveSmokeCaseV1(
                case_id=item.case_id,
                task_id=item.task_id,
                mode=item.mode,
                request_fixture_path=item.request_fixture_path,
                request_fixture_sha256=item.request_fixture_sha256,
                request_fixture_byte_count=item.request_fixture_byte_count,
                max_actor_calls=item.max_actor_calls,
                max_openai_calls=item.max_openai_calls,
                max_wall_time_seconds=item.max_wall_time_seconds,
                max_cost_usd_micros=item.max_cost_usd_micros,
                actor_action_allowed=item.actor_action_allowed,
                provider_final_request_proof_required=item.provider_final_request_proof_required,
            )
        )
    return HostLiveSmokePlanV1(host=value.host, cases=tuple(cases))


def _snapshot_pilot(value: FrozenPilotManifestV1) -> FrozenPilotManifestV1:
    if type(value) is not FrozenPilotManifestV1:
        raise ProductionDriverError("UNTRUSTED_TYPE", "pilot must use exact manifest type")
    return parse_frozen_pilot_manifest(frozen_pilot_manifest_projection(value))


def _validate_lease(value: CaseAuthorityBrokerV1, manifest_sha256: str) -> None:
    try:
        valid_protocol = isinstance(value, CaseAuthorityBrokerV1)
        lease_manifest = value.manifest_sha256
        environment_key = value.environment_key
    except Exception:
        raise ProductionDriverError(
            "INVALID_SECRET_LEASE", "opaque lease metadata unavailable"
        ) from None
    if (
        not valid_protocol
        or lease_manifest != manifest_sha256
        or environment_key != "OPENAI_API_KEY"
    ):
        raise ProductionDriverError("INVALID_SECRET_LEASE", "opaque lease binding differs")


class _PostPreflightCaseAuthorityBrokerV1:
    """No-secret stage broker over one exact post-preflight factory."""

    __slots__ = ("_closed", "_factory", "_issued", "_lock")

    def __init__(self, factory: ProductionPostPreflightFactoryV1, *, seal: object) -> None:
        if seal is not _MODULE_SEAL or type(factory) is not ProductionPostPreflightFactoryV1:
            raise PermissionError("post-preflight case broker is module-owned")
        self._factory = factory
        self._closed = False
        self._issued: dict[str, CaseExecutionLeaseV1] = {}
        self._lock = threading.Lock()

    @property
    def manifest_sha256(self) -> str:
        return self._factory.manifest_sha256

    @property
    def environment_key(self) -> str:
        return "OPENAI_API_KEY"

    def issue_case_execution_lease(
        self,
        *,
        stage: RunStageV1,
        host: PilotHostV1,
        mode: SmokeModeV1,
        case_id: str,
        task_id: str,
        task_parameters_sha256: str | None,
        reset_seed: int | None,
        actor_call_index: int,
        request_sha256: str,
    ) -> CaseExecutionLeaseBindingV1:
        with self._lock:
            if self._closed:
                raise ProductionDriverError("CASE_BROKER_CLOSED", "case broker is closed")
            lease = self._factory.issue_case_execution_lease(
                stage=stage,
                host=host,
                mode=mode,
                case_id=case_id,
                task_id=task_id,
                task_parameters_sha256=task_parameters_sha256,
                reset_seed=reset_seed,
                actor_call_index=actor_call_index,
                request_sha256=request_sha256,
            )
            trusted = self._factory.validate_case_execution_lease(lease)
            digest = case_execution_lease_sha256(trusted)
            self._issued[digest] = trusted
            return CaseExecutionLeaseBindingV1(
                manifest_sha256=trusted.manifest_sha256,
                preflight_report_sha256=trusted.preflight_report_sha256,
                factory_binding_sha256=trusted.factory_binding_sha256,
                pricing_binding_sha256=trusted.pricing_binding_sha256,
                case_execution_lease_sha256=digest,
                execution_scope=trusted.execution_scope.value,
                openai_stage_set_sha256=trusted.openai_stage_set_sha256,
                stage=trusted.stage,
                host=trusted.host,
                mode=trusted.mode,
                case_id=trusted.case_id,
                task_id=trusted.task_id,
                task_parameters_sha256=trusted.task_parameters_sha256,
                reset_seed=trusted.reset_seed,
                actor_call_index=trusted.actor_call_index,
                request_sha256=trusted.request_sha256,
                issued_at_utc=trusted.issued_at_utc,
                expires_at_utc=trusted.expires_at_utc,
            )

    def trusted_case_execution_lease(
        self, case_lease: CaseExecutionLeaseBindingV1
    ) -> CaseExecutionLeaseV1:
        """Resolve only a broker-owned authority; no secret enters this process."""

        with self._lock:
            if self._closed or type(case_lease) is not CaseExecutionLeaseBindingV1:
                raise ProductionDriverError("CASE_BROKER_CLOSED", "case broker is unavailable")
            trusted = self._issued.get(case_lease.case_execution_lease_sha256)
            if trusted is None or (
                trusted.manifest_sha256 != case_lease.manifest_sha256
                or trusted.preflight_report_sha256 != case_lease.preflight_report_sha256
                or trusted.factory_binding_sha256 != case_lease.factory_binding_sha256
                or trusted.pricing_binding_sha256 != case_lease.pricing_binding_sha256
                or trusted.execution_scope.value != case_lease.execution_scope
                or trusted.openai_stage_set_sha256 != case_lease.openai_stage_set_sha256
                or trusted.stage is not case_lease.stage
                or trusted.host is not case_lease.host
                or trusted.mode is not case_lease.mode
                or trusted.case_id != case_lease.case_id
                or trusted.task_id != case_lease.task_id
                or trusted.task_parameters_sha256 != case_lease.task_parameters_sha256
                or trusted.reset_seed != case_lease.reset_seed
                or trusted.actor_call_index != case_lease.actor_call_index
                or trusted.request_sha256 != case_lease.request_sha256
            ):
                raise ProductionDriverError(
                    "CASE_EXECUTION_LEASE_MISMATCH", "case lease is not broker-owned"
                )
            return self._factory.validate_case_execution_lease(trusted)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                raise ProductionDriverError("CASE_BROKER_CLOSED", "case broker already closed")
            self._closed = True


class ProductionCaseAuthorityBrokerProviderV1:
    """Exact provider constructed only from an authorized post-preflight factory."""

    __slots__ = ("_factory",)

    def __init__(self, factory: ProductionPostPreflightFactoryV1, *, seal: object) -> None:
        if seal is not _MODULE_SEAL or type(factory) is not ProductionPostPreflightFactoryV1:
            raise PermissionError("production case broker provider is module-owned")
        self._factory = factory

    def acquire(
        self,
        reference: SecretFileReferenceV1,
        *,
        manifest_sha256: str,
    ) -> CaseAuthorityBrokerV1:
        try:
            environment_key = getattr(reference, "environment_key")
        except Exception:
            raise ProductionDriverError(
                "INVALID_SECRET_REFERENCE", "secret reference metadata is unavailable"
            ) from None
        if manifest_sha256 != self._factory.manifest_sha256 or environment_key != "OPENAI_API_KEY":
            raise ProductionDriverError(
                "CASE_BROKER_MANIFEST_MISMATCH", "case broker manifest binding differs"
            )
        return _PostPreflightCaseAuthorityBrokerV1(self._factory, seal=_MODULE_SEAL)


def build_production_case_authority_broker_provider_v1(
    factory: ProductionPostPreflightFactoryV1,
) -> ProductionCaseAuthorityBrokerProviderV1:
    """Bind case-level lease issuance to one exact post-preflight factory."""

    if type(factory) is not ProductionPostPreflightFactoryV1:
        raise ProductionDriverError(
            "POST_PREFLIGHT_FACTORY_REQUIRED", "exact post-preflight factory is required"
        )
    return ProductionCaseAuthorityBrokerProviderV1(factory, seal=_MODULE_SEAL)


def _stage_for_host(host: PilotHostV1) -> RunStageV1:
    return RunStageV1.QWEN_LIVE_SMOKE if host is PilotHostV1.QWEN3_VL else RunStageV1.MAI_LIVE_SMOKE


def _validate_smoke_reservation(plan: HostLiveSmokePlanV1, context: StageAdapterContextV1) -> None:
    actor = sum(item.max_actor_calls for item in plan.cases)
    openai = sum(item.max_openai_calls for item in plan.cases)
    cost = sum(item.max_cost_usd_micros for item in plan.cases)
    wall = sum(item.max_wall_time_seconds for item in plan.cases) * 1000
    if any(item.max_openai_calls > 3 for item in plan.cases):
        raise ProductionDriverError(
            "INVALID_SMOKE_BUDGET",
            "smoke permits rubric generate/track plus one history-policy call per actor",
        )
    if (
        actor > context.remaining_actor_calls
        or openai > context.remaining_openai_calls
        or cost > context.remaining_cost_usd_micros
        or wall > context.remaining_wall_time_ms
    ):
        raise ProductionDriverError(
            "STAGE_RESERVATION_EXCEEDS_CONTEXT", "smoke declared budget exceeds remaining authority"
        )


def _validate_pilot_reservation(
    pilot: FrozenPilotManifestV1, context: StageAdapterContextV1
) -> None:
    if not 20 <= len(pilot.tasks) <= 30 or len(pilot.cells) != len(pilot.tasks) * 4:
        raise ProductionDriverError(
            "PILOT_MATRIX_MISMATCH",
            "driver requires 20--30 tasks and four exact cells per task",
        )
    if (
        pilot.max_total_actor_calls > context.remaining_actor_calls
        or pilot.max_total_openai_calls > context.remaining_openai_calls
        or pilot.max_total_cost_usd_micros > context.remaining_cost_usd_micros
        or pilot.max_total_wall_time_seconds * 1000 > context.remaining_wall_time_ms
    ):
        raise ProductionDriverError(
            "STAGE_RESERVATION_EXCEEDS_CONTEXT", "pilot declared budget exceeds remaining authority"
        )


@dataclass(frozen=True, slots=True)
class _SmokeInvocationV1:
    manifest_sha256: str
    run_id: str
    source_commit: str
    host: PilotHostV1
    sequence_index: int
    case: LiveSmokeCaseV1
    actor_resource_sha256: str
    history_policy_stage_sha256: str
    deadline_monotonic_ns: int
    cleanup_deadline_monotonic_ns: int
    authority_deadline_monotonic_ns: int
    attempt_termination_upper_bound_ns: int


@dataclass(frozen=True, slots=True)
class _PilotInvocationV1:
    manifest_sha256: str
    run_id: str
    source_commit: str
    sequence_index: int
    cell: PilotCellV1
    actor_resource_sha256: str
    history_policy_stage_sha256: str
    deadline_monotonic_ns: int
    cleanup_deadline_monotonic_ns: int
    authority_deadline_monotonic_ns: int
    attempt_termination_upper_bound_ns: int


@dataclass(frozen=True, slots=True)
class _SmokePortResultV1:
    request_fixture_sha256: str
    request_fixture_byte_count: int
    decision: ActorDecisionEvidenceV1


@dataclass(frozen=True, slots=True)
class _PilotResetResultV1:
    reset_receipt_sha256: str
    effective_reset_state_sha256: str


@dataclass(frozen=True, slots=True)
class _PilotPortResultV1:
    decisions: tuple[ActorDecisionEvidenceV1, ...]
    official_result: OfficialTaskResultEvidenceV1


@dataclass(frozen=True, slots=True)
class _CleanupResultV1:
    cleanup_receipt_sha256: str
    unit_journal_preimage: bytes | None = None
    unit_journal_sha256: str | None = None
    unit_journal_validated_reference: _ValidatedUnitJournalReferenceV1 | None = None

    def __post_init__(self) -> None:
        _require_sha256(self.cleanup_receipt_sha256, "cleanup_receipt_sha256")
        if (self.unit_journal_preimage is None) != (self.unit_journal_sha256 is None):
            raise ProductionDriverError(
                "INVALID_UNIT_JOURNAL", "unit journal preimage/hash presence differs"
            )
        if self.unit_journal_preimage is not None:
            assert self.unit_journal_sha256 is not None
            if (
                type(self.unit_journal_preimage) is not bytes
                or hashlib.sha256(self.unit_journal_preimage).hexdigest()
                != self.unit_journal_sha256
            ):
                raise ProductionDriverError(
                    "INVALID_UNIT_JOURNAL", "unit journal preimage/hash differs"
                )
            try:
                journal = json.loads(self.unit_journal_preimage)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProductionDriverError(
                    "INVALID_UNIT_JOURNAL", "cleanup unit journal is not JSON"
                ) from exc
            is_blob_reference = type(journal) is dict and (
                journal.get("schema_version") == _UNIT_EVIDENCE_BLOB_REFERENCE_SCHEMA_VERSION
                or journal.get("storage") == _UNIT_EVIDENCE_BLOB_STORAGE_KIND
            )
            if is_blob_reference:
                validation = self.unit_journal_validated_reference
                if type(validation) is not _ValidatedUnitJournalReferenceV1:
                    raise ProductionDriverError(
                        "INVALID_UNIT_JOURNAL", "cleanup blob journal lacks validated readback"
                    )
                validation.revalidate(
                    self.unit_journal_preimage,
                    expected_unit_id=validation.expected_unit_id,
                )
            elif self.unit_journal_validated_reference is not None:
                raise ProductionDriverError(
                    "INVALID_UNIT_JOURNAL", "cleanup inline journal has orphan blob validation"
                )
        elif self.unit_journal_validated_reference is not None:
            raise ProductionDriverError(
                "INVALID_UNIT_JOURNAL", "cleanup journal validation lacks a preimage"
            )


def _deadline_projection(
    *,
    execution_deadline: int,
    cleanup_deadline: int,
    authority_deadline: int,
    attempt_termination_upper_bound_ns: int,
) -> dict[str, JsonValue]:
    grace_ns = cleanup_deadline - execution_deadline
    teardown_budget_ns = grace_ns - attempt_termination_upper_bound_ns
    projection: dict[str, JsonValue] = {
        "attempt_termination_upper_bound_ns": attempt_termination_upper_bound_ns,
        "authority_deadline_monotonic_ns": authority_deadline,
        "cleanup_deadline_monotonic_ns": cleanup_deadline,
        "cleanup_grace_ns": grace_ns,
        "cleanup_within_owner_authority": cleanup_deadline <= authority_deadline,
        "execution_deadline_monotonic_ns": execution_deadline,
        "teardown_budget_ns": teardown_budget_ns,
        "teardown_budget_positive": teardown_budget_ns > 0,
    }
    projection["deadline_binding_sha256"] = _hash_projection(
        "production-unit-deadline-binding", cast(JsonValue, projection)
    )
    return projection


def _unit_deadline_projection(
    invocation: _SmokeInvocationV1 | _PilotInvocationV1,
) -> dict[str, JsonValue]:
    return _deadline_projection(
        execution_deadline=invocation.deadline_monotonic_ns,
        cleanup_deadline=invocation.cleanup_deadline_monotonic_ns,
        authority_deadline=invocation.authority_deadline_monotonic_ns,
        attempt_termination_upper_bound_ns=(invocation.attempt_termination_upper_bound_ns),
    )


def _freeze_unit_deadlines(
    *,
    unit_started_ns: int,
    unit_timeout_seconds: int,
    authority_deadline_monotonic_ns: int,
    shutdown_grace_seconds: int,
    attempt_termination_upper_bound_ns: int,
) -> tuple[int, int]:
    if (
        type(unit_started_ns) is not int
        or type(unit_timeout_seconds) is not int
        or unit_timeout_seconds <= 0
        or type(authority_deadline_monotonic_ns) is not int
        or type(shutdown_grace_seconds) is not int
        or not 0 <= shutdown_grace_seconds <= 60
        or type(attempt_termination_upper_bound_ns) is not int
        or attempt_termination_upper_bound_ns < 0
    ):
        raise ProductionDriverError("INVALID_DEADLINE_BINDING", "unit deadline input differs")
    cleanup_deadline = min(
        authority_deadline_monotonic_ns,
        unit_started_ns + unit_timeout_seconds * 1_000_000_000,
    )
    execution_deadline = cleanup_deadline - shutdown_grace_seconds * 1_000_000_000
    if (
        shutdown_grace_seconds > 0
        and shutdown_grace_seconds * 1_000_000_000 <= attempt_termination_upper_bound_ns
    ):
        raise ProductionDriverError(
            "INSUFFICIENT_SHUTDOWN_GRACE",
            "cleanup grace leaves no teardown budget after attempt termination",
        )
    if execution_deadline <= unit_started_ns:
        raise ProductionDriverError(
            "INSUFFICIENT_CLEANUP_WINDOW",
            "owner/unit authority cannot reserve the configured cleanup grace",
        )
    return execution_deadline, cleanup_deadline


def _exception_code(error: BaseException | None, default: str) -> str:
    if isinstance(error, ProductionDriverError) and type(error.code) is str:
        return error.code
    return default


def _cleanup_outcome_projection(
    cleanup: _CleanupResultV1 | None,
    *,
    attempted: bool,
    error: BaseException | None,
) -> dict[str, JsonValue]:
    if cleanup is not None:
        return {
            "attempted": True,
            "cleanup_receipt_sha256": cleanup.cleanup_receipt_sha256,
            "failure_code": None,
            "status": "SUCCEEDED",
        }
    return {
        "attempted": attempted,
        "cleanup_receipt_sha256": None,
        "failure_code": (
            _exception_code(error, "UNIT_CLEANUP_FAILED") if error is not None else None
        ),
        "status": "FAILED" if error is not None else "NOT_ATTEMPTED",
    }


def _smoke_current_unit_projection(
    invocation: _SmokeInvocationV1,
    port_result: _SmokePortResultV1 | None,
    cleanup: _CleanupResultV1 | None,
    *,
    cleanup_attempted: bool,
    cleanup_error: BaseException | None,
) -> dict[str, JsonValue]:
    port_projection: JsonValue | None = None
    if port_result is not None:
        port_projection = cast(
            JsonValue,
            {
                "decision": _decision_projection(port_result.decision),
                "request_fixture_byte_count": port_result.request_fixture_byte_count,
                "request_fixture_sha256": port_result.request_fixture_sha256,
            },
        )
    projection: dict[str, JsonValue] = {
        "case_id": invocation.case.case_id,
        "cleanup": _cleanup_outcome_projection(
            cleanup,
            attempted=cleanup_attempted,
            error=cleanup_error,
        ),
        "host": invocation.host.value,
        "mode": invocation.case.mode.value,
        "port_result": port_projection,
        "sequence_index": invocation.sequence_index,
        "task_id": invocation.case.task_id,
        "unit_deadline": cast(JsonValue, _unit_deadline_projection(invocation)),
    }
    projection["canonical_evidence_sha256"] = _hash_projection(
        "smoke-current-unit-failure-journal", cast(JsonValue, projection)
    )
    return projection


def _pilot_current_unit_projection(
    invocation: _PilotInvocationV1,
    reset: _PilotResetResultV1 | None,
    port_result: _PilotPortResultV1 | None,
    cleanup: _CleanupResultV1 | None,
    *,
    cleanup_attempted: bool,
    cleanup_error: BaseException | None,
) -> dict[str, JsonValue]:
    reset_projection: JsonValue | None = None
    if reset is not None:
        reset_projection = cast(
            JsonValue,
            {
                "effective_reset_state_sha256": reset.effective_reset_state_sha256,
                "reset_receipt_sha256": reset.reset_receipt_sha256,
            },
        )
    port_projection: JsonValue | None = None
    if port_result is not None:
        official = port_result.official_result
        port_projection = cast(
            JsonValue,
            {
                "decisions": [_decision_projection(item) for item in port_result.decisions],
                "official_result": {
                    "evaluator_id": official.evaluator_id,
                    "reason_sha256": official.reason_sha256,
                    "result_payload_sha256": official.result_payload_sha256,
                    "score_ppm": official.score_ppm,
                    "successful": official.successful,
                    "task_id": official.task_id,
                },
            },
        )
    projection: dict[str, JsonValue] = {
        "arm": invocation.cell.arm.value,
        "cleanup": _cleanup_outcome_projection(
            cleanup,
            attempted=cleanup_attempted,
            error=cleanup_error,
        ),
        "host": invocation.cell.host.value,
        "port_result": port_projection,
        "reset_result": reset_projection,
        "sequence_index": invocation.sequence_index,
        "sentinel_mode": invocation.cell.sentinel_mode,
        "task_id": invocation.cell.task_id,
        "unit_deadline": cast(JsonValue, _unit_deadline_projection(invocation)),
    }
    projection["canonical_evidence_sha256"] = _hash_projection(
        "pilot-current-unit-failure-journal", cast(JsonValue, projection)
    )
    return projection


def _smoke_unit_failure_preimage(
    *,
    context: StageAdapterContextV1,
    stage: RunStageV1,
    records: list[SmokeCaseEvidenceV1],
    invocation: _SmokeInvocationV1,
    port_result: _SmokePortResultV1 | None,
    cleanup: _CleanupResultV1 | None,
    cleanup_attempted: bool,
    cleanup_error: BaseException | None,
    dispatch_error: BaseException | None,
    failure_phase: str,
    failure_code: str,
    unit_failure_evidence: JsonValue | None,
) -> bytes:
    completed_records = [_smoke_case_evidence_projection(item) for item in records]
    return canonical_json_bytes(
        cast(
            JsonValue,
            {
                "completed_case_ids": [item.case_id for item in records],
                "completed_records": completed_records,
                "completed_records_sha256": _hash_projection(
                    "smoke-completed-unit-journal", cast(JsonValue, completed_records)
                ),
                "current_unit": _smoke_current_unit_projection(
                    invocation,
                    port_result,
                    cleanup,
                    cleanup_attempted=cleanup_attempted,
                    cleanup_error=cleanup_error,
                ),
                "dispatch_failure_code": (
                    _exception_code(dispatch_error, "SMOKE_CASE_EXECUTION_FAILED")
                    if dispatch_error is not None
                    else None
                ),
                "failed_case_id": invocation.case.case_id,
                "failed_sequence_index": invocation.sequence_index,
                "failure_code": failure_code,
                "failure_phase": failure_phase,
                "manifest_sha256": context.manifest_sha256,
                "run_id": context.run_id,
                "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                "stage": stage.value,
                "status": "FAILED",
                "unit_failure_evidence": unit_failure_evidence,
            },
        )
    )


def _pilot_unit_failure_preimage(
    *,
    context: StageAdapterContextV1,
    records: list[PilotCellEvidenceV1],
    invocation: _PilotInvocationV1,
    reset: _PilotResetResultV1 | None,
    port_result: _PilotPortResultV1 | None,
    cleanup: _CleanupResultV1 | None,
    cleanup_attempted: bool,
    cleanup_error: BaseException | None,
    dispatch_error: BaseException | None,
    failure_phase: str,
    failure_code: str,
    unit_failure_evidence: JsonValue | None,
) -> bytes:
    completed_records = [_pilot_cell_evidence_projection(item) for item in records]
    return canonical_json_bytes(
        cast(
            JsonValue,
            {
                "completed_cell_indices": [item.sequence_index for item in records],
                "completed_records": completed_records,
                "completed_records_sha256": _hash_projection(
                    "pilot-completed-unit-journal", cast(JsonValue, completed_records)
                ),
                "current_unit": _pilot_current_unit_projection(
                    invocation,
                    reset,
                    port_result,
                    cleanup,
                    cleanup_attempted=cleanup_attempted,
                    cleanup_error=cleanup_error,
                ),
                "dispatch_failure_code": (
                    _exception_code(dispatch_error, "PILOT_CELL_EXECUTION_FAILED")
                    if dispatch_error is not None
                    else None
                ),
                "failed_cell_index": invocation.sequence_index,
                "failed_task_id": invocation.cell.task_id,
                "failure_code": failure_code,
                "failure_phase": failure_phase,
                "manifest_sha256": context.manifest_sha256,
                "run_id": context.run_id,
                "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                "stage": RunStageV1.R25_PILOT.value,
                "status": "FAILED",
                "unit_failure_evidence": unit_failure_evidence,
            },
        )
    )


@dataclass(frozen=True, slots=True)
class CpuProductionDriverTraceV1:
    events: tuple[str, ...]
    smoke_dispatches: int
    pilot_resets: int
    pilot_dispatches: int
    cleanup_attempts: int


class _CpuFixedExecutionPortV1:
    """The sole installed port: deterministic, data-only, and I/O-free."""

    __slots__ = ("_events", "_fault", "_lock")

    def __init__(self, fault: CpuProductionDriverFaultV1, *, seal: object) -> None:
        if seal is not _MODULE_SEAL or type(fault) is not CpuProductionDriverFaultV1:
            raise ValueError("CPU execution port is module-owned")
        self._fault = fault
        self._events: list[str] = []
        self._lock = threading.Lock()

    def _record(self, event: str) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def shutdown_grace_seconds(self) -> int:
        return 0

    @property
    def attempt_termination_upper_bound_ns(self) -> int:
        return 0

    @staticmethod
    def _unit_id(invocation: _SmokeInvocationV1 | _PilotInvocationV1) -> str:
        if type(invocation) is _SmokeInvocationV1:
            return f"smoke:{invocation.host.value}:{invocation.case.mode.value}"
        return f"pilot:{invocation.sequence_index:03d}"

    def _decision(
        self,
        *,
        manifest_sha256: str,
        run_id: str,
        unit_id: str,
        actor_call_index: int,
        semantic_mode: bool,
        transform_request: bool,
        execute_action: bool,
        stage: RunStageV1,
        host: PilotHostV1,
        mode: SmokeModeV1,
        case_id: str,
        task_id: str,
        task_parameters_sha256: str | None,
        reset_seed: int | None,
        broker: CaseAuthorityBrokerV1,
    ) -> ActorDecisionEvidenceV1:
        binding: JsonValue = {
            "actor_call_index": actor_call_index,
            "manifest_sha256": manifest_sha256,
            "run_id": run_id,
            "unit_id": unit_id,
        }
        raw = _hash_projection("cpu-raw-request", binding)
        final = _hash_projection("cpu-final-request", binding) if transform_request else raw
        parsed_action = _hash_projection("cpu-parsed-action", binding)
        rubric_calls = 2 if semantic_mode else 0
        history_calls = 1 if semantic_mode else 0
        openai_calls = rubric_calls + history_calls
        logical_call_id = f"cpu-{_hash_projection('cpu-logical-call', binding)[:32]}"
        case_lease: CaseExecutionLeaseBindingV1 | None = None
        if semantic_mode:
            case_lease = broker.issue_case_execution_lease(
                stage=stage,
                host=host,
                mode=mode,
                case_id=case_id,
                task_id=task_id,
                task_parameters_sha256=task_parameters_sha256,
                reset_seed=reset_seed,
                actor_call_index=actor_call_index,
                request_sha256=raw,
            )
            if (
                type(case_lease) is not CaseExecutionLeaseBindingV1
                or case_lease.manifest_sha256 != manifest_sha256
                or case_lease.stage is not stage
                or case_lease.host is not host
                or case_lease.mode is not mode
                or case_lease.case_id != case_id
                or case_lease.task_id != task_id
                or case_lease.task_parameters_sha256 != task_parameters_sha256
                or case_lease.reset_seed != reset_seed
                or case_lease.actor_call_index != actor_call_index
                or case_lease.request_sha256 != raw
            ):
                raise ProductionDriverError(
                    "CASE_EXECUTION_LEASE_MISMATCH", "case broker returned a mismatched lease"
                )
        history_attempt_sha256: str | None = None
        if semantic_mode:
            history_attempt_sha256 = _hash_projection("cpu-history-policy-attempt", binding)
        census = DriverCallCensusV1(
            actor_calls=1,
            offline_rubric_evaluations=0,
            rubric_openai_calls=rubric_calls,
            history_policy_openai_calls=history_calls,
            openai_calls=openai_calls,
            actor_actions=1 if execute_action else 0,
            cost_usd_micros=0,
            wall_time_ms=1,
        )
        return ActorDecisionEvidenceV1(
            logical_call_id=logical_call_id,
            actor_call_index=actor_call_index,
            raw_request_sha256=raw,
            final_request_sha256=final,
            provider_request_sha256=final,
            provider_response_sha256=_hash_projection("cpu-provider-response", binding),
            exact_diff_sha256=_hash_projection(
                "cpu-exact-diff", cast(JsonValue, {"final": final, "raw": raw})
            ),
            pre_provider_status=(
                ProductionRuntimeAuditPreProviderStatusV1.READY
                if semantic_mode
                else ProductionRuntimeAuditPreProviderStatusV1.OFF
            ),
            pre_provider_outcome=(
                ProductionRuntimeAuditPreProviderOutcomeV1.READY
                if semantic_mode
                else ProductionRuntimeAuditPreProviderOutcomeV1.OFF
            ),
            fallback_reason=None,
            fallback_check=None,
            preflight_report_sha256=(
                case_lease.preflight_report_sha256
                if case_lease is not None
                else _hash_projection("cpu-preflight", manifest_sha256)
            ),
            case_execution_lease_sha256=(
                None if case_lease is None else case_lease.case_execution_lease_sha256
            ),
            live_policy_factory_binding_sha256=(
                case_lease.factory_binding_sha256
                if case_lease is not None
                else _hash_projection("cpu-factory", manifest_sha256)
            ),
            live_policy_authority_sha256=(
                _hash_projection("cpu-live-policy-authority", binding) if semantic_mode else None
            ),
            rubric_attempt_receipt_sha256s=tuple(
                _hash_projection(
                    "cpu-rubric-attempt",
                    cast(JsonValue, {"binding": binding, "rubric_call_index": index}),
                )
                for index in range(1, rubric_calls + 1)
            ),
            history_policy_attempt_receipt_sha256=history_attempt_sha256,
            actor_attempt_receipt_sha256=_hash_projection("cpu-actor-attempt", binding),
            sentinel_receipt_sha256=_hash_projection("cpu-sentinel-receipt", binding),
            provider_attempt_receipt_sha256=_hash_projection("cpu-provider-attempt", binding),
            runtime_audit_detail_sha256=_hash_projection("cpu-runtime-audit", binding),
            parser_result_sha256=_hash_projection("cpu-parser-result", binding),
            parsed_action_sha256=parsed_action,
            executed_action_sha256=parsed_action if execute_action else None,
            census=census,
        )

    def run_smoke_case(
        self,
        invocation: _SmokeInvocationV1,
        lease: CaseAuthorityBrokerV1,
    ) -> _SmokePortResultV1:
        unit_id = self._unit_id(invocation)
        self._record(f"RUN:{unit_id}")
        if (
            self._fault is CpuProductionDriverFaultV1.SMOKE_SHADOW_DISPATCH_FAILURE
            and invocation.case.mode is SmokeModeV1.SHADOW
        ):
            raise RuntimeError("private CPU smoke failure")
        semantic = invocation.case.mode is not SmokeModeV1.OFF
        request_fixture_sha256 = invocation.case.request_fixture_sha256
        if (
            self._fault is CpuProductionDriverFaultV1.SMOKE_SHADOW_POST_DISPATCH_ADMISSION_FAILURE
            and invocation.case.mode is SmokeModeV1.SHADOW
        ):
            request_fixture_sha256 = _hash_projection(
                "cpu-smoke-post-dispatch-fixture-drift",
                cast(JsonValue, {"unit_id": unit_id}),
            )
        return _SmokePortResultV1(
            request_fixture_sha256=request_fixture_sha256,
            request_fixture_byte_count=invocation.case.request_fixture_byte_count,
            decision=self._decision(
                manifest_sha256=invocation.manifest_sha256,
                run_id=invocation.run_id,
                unit_id=unit_id,
                actor_call_index=1,
                semantic_mode=semantic,
                transform_request=invocation.case.mode is SmokeModeV1.ACTIVE,
                execute_action=False,
                stage=_stage_for_host(invocation.host),
                host=invocation.host,
                mode=invocation.case.mode,
                case_id=invocation.case.case_id,
                task_id=invocation.case.task_id,
                task_parameters_sha256=None,
                reset_seed=None,
                broker=lease,
            ),
        )

    def reset_pilot_cell(self, invocation: _PilotInvocationV1) -> _PilotResetResultV1:
        unit_id = self._unit_id(invocation)
        self._record(f"RESET:{unit_id}")
        if (
            self._fault is CpuProductionDriverFaultV1.PILOT_CELL_007_RESET_FAILURE
            and invocation.sequence_index == 7
        ):
            raise RuntimeError("private CPU reset failure")
        return _PilotResetResultV1(
            reset_receipt_sha256=_hash_projection(
                "cpu-pilot-reset",
                cast(
                    JsonValue,
                    {
                        "cell_index": invocation.sequence_index,
                        "manifest_sha256": invocation.manifest_sha256,
                        "reset_seed": invocation.cell.reset_seed,
                        "task_id": invocation.cell.task_id,
                        "task_parameters_sha256": invocation.cell.task_parameters_sha256,
                    },
                ),
            ),
            effective_reset_state_sha256=_hash_projection(
                "cpu-pilot-effective-reset-state",
                cast(
                    JsonValue,
                    {
                        "drift": (
                            invocation.sequence_index
                            if self._fault
                            is CpuProductionDriverFaultV1.PILOT_CELL_001_RESET_STATE_DRIFT
                            and invocation.sequence_index == 1
                            else None
                        ),
                        "reset_seed": invocation.cell.reset_seed,
                        "task_id": invocation.cell.task_id,
                        "task_parameters_sha256": invocation.cell.task_parameters_sha256,
                    },
                ),
            ),
        )

    def prepare_pilot(self, pilot: FrozenPilotManifestV1) -> None:
        if type(pilot) is not FrozenPilotManifestV1:
            raise ProductionDriverError("UNTRUSTED_TYPE", "pilot manifest type differs")

    def run_pilot_cell(
        self,
        invocation: _PilotInvocationV1,
        lease: CaseAuthorityBrokerV1,
    ) -> _PilotPortResultV1:
        unit_id = self._unit_id(invocation)
        self._record(f"RUN:{unit_id}")
        if (
            self._fault is CpuProductionDriverFaultV1.PILOT_CELL_007_DISPATCH_FAILURE
            and invocation.sequence_index == 7
        ):
            raise RuntimeError("private CPU pilot failure")
        semantic = invocation.cell.arm is PilotArmV1.JOINT_SENTINEL
        decision = self._decision(
            manifest_sha256=invocation.manifest_sha256,
            run_id=invocation.run_id,
            unit_id=unit_id,
            actor_call_index=1,
            semantic_mode=semantic,
            transform_request=semantic,
            execute_action=True,
            stage=RunStageV1.R25_PILOT,
            host=invocation.cell.host,
            mode=(
                SmokeModeV1.OFF
                if invocation.cell.arm is PilotArmV1.BASELINE
                else SmokeModeV1.ACTIVE
            ),
            case_id=f"pilot-cell-{invocation.sequence_index:03d}",
            task_id=invocation.cell.task_id,
            task_parameters_sha256=invocation.cell.task_parameters_sha256,
            reset_seed=invocation.cell.reset_seed,
            broker=lease,
        )
        score = 1_000_000 if invocation.sequence_index % 2 == 0 else 0
        official_binding: JsonValue = {
            "evaluator_id": OFFICIAL_RESULT_EVALUATOR_ID_V1,
            "score_ppm": score,
            "task_id": invocation.cell.task_id,
            "unit_id": unit_id,
        }
        official = OfficialTaskResultEvidenceV1(
            task_id=(
                "post-dispatch-admission-drift"
                if self._fault
                is CpuProductionDriverFaultV1.PILOT_CELL_007_POST_DISPATCH_ADMISSION_FAILURE
                and invocation.sequence_index == 7
                else invocation.cell.task_id
            ),
            evaluator_id=OFFICIAL_RESULT_EVALUATOR_ID_V1,
            score_ppm=score,
            successful=score == 1_000_000,
            result_payload_sha256=_hash_projection("cpu-official-result", official_binding),
            reason_sha256=_hash_projection("cpu-official-reason", official_binding),
        )
        return _PilotPortResultV1(decisions=(decision,), official_result=official)

    def cleanup_unit(self, invocation: _SmokeInvocationV1 | _PilotInvocationV1) -> _CleanupResultV1:
        unit_id = self._unit_id(invocation)
        self._record(f"CLEANUP:{unit_id}")
        smoke_cleanup_fault = (
            type(invocation) is _SmokeInvocationV1
            and invocation.case.mode is SmokeModeV1.SHADOW
            and self._fault is CpuProductionDriverFaultV1.SMOKE_SHADOW_CLEANUP_FAILURE
        )
        pilot_cleanup_fault = (
            type(invocation) is _PilotInvocationV1
            and invocation.sequence_index == 7
            and self._fault is CpuProductionDriverFaultV1.PILOT_CELL_007_CLEANUP_FAILURE
        )
        if smoke_cleanup_fault or pilot_cleanup_fault:
            raise RuntimeError("private CPU cleanup failure")
        return _CleanupResultV1(
            cleanup_receipt_sha256=_hash_projection(
                "cpu-unit-cleanup",
                cast(
                    JsonValue,
                    {
                        "deadline_binding": cast(JsonValue, _unit_deadline_projection(invocation)),
                        "manifest_sha256": invocation.manifest_sha256,
                        "unit_id": unit_id,
                    },
                ),
            )
        )

    @property
    def trace(self) -> CpuProductionDriverTraceV1:
        with self._lock:
            events = tuple(self._events)
        return CpuProductionDriverTraceV1(
            events=events,
            smoke_dispatches=sum(event.startswith("RUN:smoke:") for event in events),
            pilot_resets=sum(event.startswith("RESET:pilot:") for event in events),
            pilot_dispatches=sum(event.startswith("RUN:pilot:") for event in events),
            cleanup_attempts=sum(event.startswith("CLEANUP:") for event in events),
        )


@dataclass(slots=True)
class _ProductionUnitStateV1:
    unit_id: str
    host: PilotHostV1
    task_name: str
    deadline_monotonic_ns: int
    cleanup_deadline_monotonic_ns: int
    authority_deadline_monotonic_ns: int
    attempt_termination_upper_bound_ns: int
    environment: AndroidEnvClient | None
    observation: Observation | None
    task_input: PilotResetTaskInitInputV1 | None = None
    task_goal: str | None = None
    lifecycle: AuditLifecycle | None = None
    task_binding: TaskAuditBinding | None = None
    agent: BaseAgent | None = None
    policy: OwnerAuthorizedLivePerCallPolicyV1 | None = None
    runtime_audit: ProductionRuntimeAuditV1 | None = None
    final_step_index: int = 0
    score: float | None = None
    score_reason: str | None = None
    completed: bool = False
    decision_journal: list[ActorDecisionEvidenceV1] = field(default_factory=list)
    terminal_audit_journal: list[dict[str, JsonValue]] = field(default_factory=list)
    terminal_audit_sha256s: set[str] = field(default_factory=set)
    cleanup_recovery_outcome: dict[str, JsonValue] | None = None
    resource_dispatch_journal: list[dict[str, JsonValue]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _UnpublishedUnitEvidenceV1:
    """Exact in-memory recovery preimage retained only after publication failure."""

    unit_id: str
    raw: bytes
    raw_sha256: str
    byte_count: int
    publication_failure_code: str
    durably_republished: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.unit_id) is not str
            or not self.unit_id
            or type(self.raw) is not bytes
            or type(self.byte_count) is not int
            or self.byte_count != len(self.raw)
            or self.byte_count <= _MAX_INLINE_UNIT_EVIDENCE_JOURNAL_BYTES
            or _SHA256.fullmatch(self.raw_sha256) is None
            or hashlib.sha256(self.raw).hexdigest() != self.raw_sha256
            or type(self.publication_failure_code) is not str
            or not self.publication_failure_code
            or type(self.durably_republished) is not bool
        ):
            raise ProductionDriverError(
                "UNIT_EVIDENCE_RECOVERY_INVALID",
                "unpublished unit evidence recovery state differs",
            )


def _pil_png_bytes(image: object) -> bytes:
    if not isinstance(image, Image.Image):
        raise ProductionDriverError("INVALID_OBSERVATION", "observation screenshot is not PIL")
    output = io.BytesIO()
    image.save(output, format="PNG")
    raw = output.getvalue()
    if not raw or len(raw) > 40 * 1024 * 1024:
        raise ProductionDriverError("INVALID_OBSERVATION", "screenshot bytes exceed bound")
    return raw


class _ProductionFixedExecutionPortV1:
    """Exact production host/environment owner; tests never execute this port."""

    __slots__ = (
        "_audit_sink",
        "_budget_ledger",
        "_config",
        "_factory",
        "_lock",
        "_manifest",
        "_pilot_inputs",
        "_pricing",
        "_resource_lifecycle",
        "_resources",
        "_run_fatal_latch",
        "_sentinel_receipt_sink",
        "_unit_journals",
        "_unpublished_unit_evidence",
        "_units",
    )

    def __init__(
        self,
        *,
        factory: ProductionPostPreflightFactoryV1,
        config: ProductionRuntimeConfigV1,
        pricing: LiveAttemptPricingV1,
        audit_sink: ExternalProductionRuntimeAuditSinkV1,
        budget_ledger: ProductionLiveBudgetLedgerV1,
        resource_lifecycle: ProductionResourceLifecycleAdapterV1,
        seal: object,
    ) -> None:
        if (
            seal is not _PRODUCTION_INSTALLATION_SEAL
            or type(factory) is not ProductionPostPreflightFactoryV1
            or type(config) is not ProductionRuntimeConfigV1
            or type(pricing) is not LiveAttemptPricingV1
            or type(audit_sink) is not ExternalProductionRuntimeAuditSinkV1
            or type(budget_ledger) is not ProductionLiveBudgetLedgerV1
            or type(resource_lifecycle) is not ProductionResourceLifecycleAdapterV1
        ):
            raise PermissionError("production execution port is module-owned")
        manifest = factory.manifest_snapshot()
        if (
            live_attempt_pricing_sha256(pricing) != factory.pricing_binding_sha256
            or manifest.source_commit == ""
        ):
            raise ProductionDriverError(
                "PRODUCTION_AUTHORITY_MISMATCH", "factory/pricing authority differs"
            )
        self._factory = factory
        self._config = config
        self._pricing = pricing
        self._audit_sink = audit_sink
        self._budget_ledger = budget_ledger
        self._resource_lifecycle = resource_lifecycle
        self._run_fatal_latch = build_production_run_fatal_latch_v1()
        self._manifest = manifest
        _attest_openai_sdk_version(manifest.openai_stages)
        self._resources = {item.host: item for item in manifest.actor_resources}
        self._pilot_inputs: ResolvedPilotTaskInputsV1 | None = None
        self._sentinel_receipt_sink: ExternalSentinelReceiptSink | None = None
        self._units: dict[str, _ProductionUnitStateV1] = {}
        self._unit_journals: dict[str, bytes] = {}
        self._unpublished_unit_evidence: dict[str, _UnpublishedUnitEvidenceV1] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _unit_id(invocation: _SmokeInvocationV1 | _PilotInvocationV1) -> str:
        if type(invocation) is _SmokeInvocationV1:
            return f"smoke:{invocation.host.value}:{invocation.case.mode.value}"
        return f"pilot:{invocation.sequence_index:03d}"

    def _require_deadline(self, deadline_ns: int) -> int:
        now = time.monotonic_ns()
        if type(deadline_ns) is not int or deadline_ns <= now:
            raise ProductionDriverError("CASE_DEADLINE_EXCEEDED", "case wall deadline elapsed")
        return deadline_ns

    def _require_pilot_scope_authorized(self) -> None:
        if self._factory.sequence_execution_scope.value != "R24_R25_FULL":
            raise ProductionDriverError(
                "PILOT_SCOPE_UNAUTHORIZED",
                "smoke-only authority cannot enter pilot execution",
            )

    @property
    def shutdown_grace_seconds(self) -> int:
        return self._config.shutdown_grace_seconds

    @property
    def attempt_termination_upper_bound_ns(self) -> int:
        return _PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1

    def _require_invocation_deadlines(
        self, invocation: _SmokeInvocationV1 | _PilotInvocationV1
    ) -> None:
        execution_deadline = invocation.deadline_monotonic_ns
        cleanup_deadline = invocation.cleanup_deadline_monotonic_ns
        authority_deadline = invocation.authority_deadline_monotonic_ns
        attempt_termination_bound = invocation.attempt_termination_upper_bound_ns
        grace_ns = self._config.shutdown_grace_seconds * 1_000_000_000
        if (
            type(execution_deadline) is not int
            or type(cleanup_deadline) is not int
            or cleanup_deadline <= execution_deadline
            or cleanup_deadline - execution_deadline > grace_ns
            or cleanup_deadline > authority_deadline
            or attempt_termination_bound != _PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1
            or cleanup_deadline - execution_deadline <= attempt_termination_bound
        ):
            raise ProductionDriverError(
                "INVALID_DEADLINE_BINDING", "unit execution/cleanup deadline binding differs"
            )
        self._require_deadline(execution_deadline)

    def _require_run_dispatch_allowed(self) -> None:
        try:
            self._run_fatal_latch.require_clear()
        except ProductionRunFatalError as exc:
            raise ProductionDriverError(exc.code, str(exc)) from exc

    def _require_resource_dispatch(
        self,
        host: PilotHostV1,
        kind: ProductionDispatchKindV1,
        *,
        deadline_ns: int,
        state: _ProductionUnitStateV1 | None = None,
    ) -> None:
        try:
            self._require_deadline(deadline_ns)
        except ProductionDriverError as exc:
            if kind is ProductionDispatchKindV1.CLEANUP:
                raise ProductionDriverError(
                    "CLEANUP_DEADLINE_EXCEEDED", "unit cleanup authority elapsed"
                ) from exc
            raise
        if kind is not ProductionDispatchKindV1.CLEANUP:
            self._require_run_dispatch_allowed()
        if type(self._resource_lifecycle) is ProductionResourceLifecycleAdapterV1 and state is None:
            raise ProductionDriverError(
                "RESOURCE_DISPATCH_EVIDENCE_INVALID",
                "production resource dispatch requires a unit evidence journal",
            )
        try:
            dispatch_sha256 = self._resource_lifecycle.require_dispatch(
                host,
                kind,
                authority_deadline_monotonic_ns=deadline_ns,
            )
        except Exception:
            failure_evidence = getattr(
                self._resource_lifecycle,
                "last_dispatch_failure_evidence_preimage",
                None,
            )
            if callable(failure_evidence):
                raw = failure_evidence()
                if raw is not None and state is not None:
                    state.resource_dispatch_journal.append(
                        cast(dict[str, JsonValue], json.loads(raw))
                    )
            raise
        if type(self._resource_lifecycle) is not ProductionResourceLifecycleAdapterV1:
            return
        raw = self._resource_lifecycle.last_dispatch_evidence_preimage()
        if raw is None or hashlib.sha256(raw).hexdigest() != dispatch_sha256:
            raise ProductionDriverError(
                "RESOURCE_DISPATCH_EVIDENCE_INVALID",
                "resource dispatch evidence hash differs",
            )
        assert state is not None
        state.resource_dispatch_journal.append(cast(dict[str, JsonValue], json.loads(raw)))

    def _require_broker(self, broker: CaseAuthorityBrokerV1) -> None:
        if type(broker) is not _PostPreflightCaseAuthorityBrokerV1 or (
            broker.manifest_sha256 != self._factory.manifest_sha256
        ):
            raise ProductionDriverError(
                "CASE_BROKER_AUTHORITY_MISMATCH", "production case broker differs"
            )

    def _receipt_sink(self) -> ExternalSentinelReceiptSink:
        with self._lock:
            if self._sentinel_receipt_sink is not None:
                return self._sentinel_receipt_sink
            root = Path(self._config.process_log_root)
            try:
                info = root.lstat()
            except OSError as exc:
                raise ProductionDriverError(
                    "INVALID_LOG_ROOT", "process log root is absent"
                ) from exc
            if (
                root.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.geteuid()
            ):
                raise ProductionDriverError(
                    "INVALID_LOG_ROOT", "process log root must be owner-only 0700"
                )
            sink_root = root / "sentinel-receipts"
            self._sentinel_receipt_sink = ExternalSentinelReceiptSink(
                sink_root,
                repository_root=Path(self._config.repository_root),
            )
            return self._sentinel_receipt_sink

    def _new_lifecycle(
        self,
        *,
        invocation: _SmokeInvocationV1 | _PilotInvocationV1,
        agent: BaseAgent,
        environment: object,
    ) -> tuple[AuditLifecycle, TaskAuditBinding]:
        collector_root = Path(self._config.process_log_root) / "collector"
        if isinstance(invocation, _SmokeInvocationV1):
            host = invocation.host
            task_name = invocation.case.task_id
        else:
            host = invocation.cell.host
            task_name = invocation.cell.task_id
        lifecycle = bootstrap_audit_run(
            AuditConfig(enabled=True, log_root=collector_root, store_stream_chunks=True),
            repository_root=self._config.repository_root,
            repository_commit=invocation.source_commit,
            repository_dirty=False,
            resolved_cli_config={
                "authority_manifest_sha256": invocation.manifest_sha256,
                "unit_id": self._unit_id(invocation),
            },
            resolved_agent_runtime_config={"sentinel_production": True},
            agent_type=type(agent).__qualname__,
            model_name=self._resources[host].served_model_id,
            suite_family="mobile_world",
            environment_image=_MOBILEWORLD_BACKEND_IMAGE,
            worker_id="r24-r25-production-driver",
        )
        if type(lifecycle) is not AuditLifecycle:
            raise ProductionDriverError(
                "COLLECTOR_BOOTSTRAP_FAILED", "production Collector did not initialize"
            )
        binding = lifecycle.start_task_attempt(
            task_name=task_name,
            task_index=invocation.sequence_index + 1,
            suite_family="mobile_world",
            agent=agent,
            environment=environment,
            whole_task_attempt_index=1,
        )
        if type(binding) is not TaskAuditBinding:
            lifecycle.close()
            raise ProductionDriverError(
                "COLLECTOR_TASK_BINDING_FAILED", "Collector task stream is unavailable"
            )
        if binding.metadata.task_run_id != binding.task_recorder.task_run_id:
            lifecycle.close()
            raise ProductionDriverError(
                "COLLECTOR_TASK_BINDING_FAILED", "Collector task identity differs"
            )
        return lifecycle, binding

    @staticmethod
    def _agent_endpoint(resource: SnapshotResourceV1) -> str:
        _, _, path = _exact_loopback_endpoint(
            resource.actor_endpoint, permitted_paths=frozenset({"", "/v1"})
        )
        return resource.actor_endpoint if path == "/v1" else f"{resource.actor_endpoint}/v1"

    def _new_agent(
        self,
        *,
        host: PilotHostV1,
        sentinel: PromptSentinel,
        deadline_ns: int,
    ) -> BaseAgent:
        resource = self._resources[host]
        endpoint = self._agent_endpoint(resource)
        if host is PilotHostV1.QWEN3_VL:
            agent: BaseAgent = Qwen3VLAgentMCP(
                model_name=resource.served_model_id,
                llm_base_url=endpoint,
                api_key="empty",
                observation_type="screenshot",
                runtime_conf={"temperature": 0.0},
                tools=[],
                prompt_sentinel=sentinel,
            )
        else:
            agent = MAIUINaivigationAgent(
                llm_base_url=endpoint,
                model_name=resource.served_model_id,
                api_key="empty",
                runtime_conf={
                    "history_n": 3,
                    "temperature": 0.0,
                    "top_k": -1,
                    "top_p": 1.0,
                    "max_tokens": 2048,
                },
                tools=[],
                prompt_sentinel=sentinel,
            )
        old_client = getattr(agent, "openai_client", None)
        close = getattr(old_client, "close", None)
        if callable(close):
            close()
        remaining_seconds = max(0.001, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        agent.openai_client = OpenAI(
            base_url=endpoint,
            api_key="empty",
            max_retries=0,
            http_client=DefaultHttpxClient(
                trust_env=False,
                timeout=min(120.0, remaining_seconds),
            ),
        )
        return agent

    def _new_sentinel(
        self,
        *,
        stage: RunStageV1,
        host: PilotHostV1,
        mode: SmokeModeV1,
        case_id: str,
        deadline_ns: int,
    ) -> tuple[PromptSentinel, OwnerAuthorizedLivePerCallPolicyV1 | None, ProductionRuntimeAuditV1]:
        # Re-attest the installed distribution on every actor call boundary;
        # stage declarations alone are not evidence of the imported SDK.
        _attest_openai_sdk_version(self._manifest.openai_stages)
        if mode is SmokeModeV1.OFF:
            policy: NoOpSentinelPolicy | OwnerAuthorizedLivePerCallPolicyV1 = NoOpSentinelPolicy()
            live_policy = None
        else:
            live_policy = build_owner_authorized_live_per_call_policy_v1(
                factory=self._factory,
                pricing=self._pricing,
                budget_ledger=self._budget_ledger,
                stage=stage,
                host=host,
                mode=mode,
                case_id=case_id,
                case_deadline_monotonic_ns=deadline_ns,
            )
            policy = live_policy
        audit = ProductionRuntimeAuditV1(
            policy=live_policy,
            sink=self._audit_sink,
            run_fatal_latch=self._run_fatal_latch,
        )
        timeout_ms = max(1, (deadline_ns - time.monotonic_ns()) // 1_000_000)
        config = SentinelHostConfig(
            mode=SentinelMode(mode.value),
            history_codec_contract_version="v1",
            policy_timeout_ms=timeout_ms,
        )
        sentinel = PromptSentinel(
            policy=policy,
            codec_registry=build_runtime_history_codec_resolver(),
            host_configs={OwnerAuthorizedLivePerCallPolicyV1._host_id(host): config},
            default_host_config=SentinelHostConfig(mode=SentinelMode.OFF),
            receipt_sink=self._receipt_sink(),
            runtime_audit=audit,
        )
        return sentinel, live_policy, audit

    def _begin_task_runtime(
        self,
        *,
        invocation: _SmokeInvocationV1 | _PilotInvocationV1,
        state: _ProductionUnitStateV1,
        task_goal: str,
        initial_observation: Observation,
    ) -> None:
        self._require_deadline(invocation.deadline_monotonic_ns)
        self._require_run_dispatch_allowed()
        if isinstance(invocation, _SmokeInvocationV1):
            host = invocation.host
            mode = invocation.case.mode
            stage = _stage_for_host(host)
            case_id = invocation.case.case_id
        else:
            host = invocation.cell.host
            mode = (
                SmokeModeV1.OFF
                if invocation.cell.arm is PilotArmV1.BASELINE
                else SmokeModeV1.ACTIVE
            )
            stage = RunStageV1.R25_PILOT
            case_id = f"pilot-cell-{invocation.sequence_index:03d}"
        sentinel, policy, runtime_audit = self._new_sentinel(
            stage=stage,
            host=host,
            mode=mode,
            case_id=case_id,
            deadline_ns=invocation.deadline_monotonic_ns,
        )
        agent = self._new_agent(
            host=host, sentinel=sentinel, deadline_ns=invocation.deadline_monotonic_ns
        )
        agent.initialize(task_goal)
        lifecycle, binding = self._new_lifecycle(
            invocation=invocation,
            agent=agent,
            environment=state.environment if state.environment is not None else {"smoke": True},
        )
        started = binding.capture.start_task(
            task_name=state.task_name,
            task_goal=task_goal,
            task_goal_status="resolved",
            task_index=invocation.sequence_index + 1,
            suite_family="mobile_world",
            agent={"host": host.value, "model": self._resources[host].served_model_id},
            environment={
                "backend": "fixture-only" if state.environment is None else "mobileworld",
                "device": self._config.backend_device,
            },
            whole_task_attempt_index=1,
        )
        if started is None or not binding.capture.capture_complete:
            lifecycle.finish_task_attempt(
                binding=binding,
                result=None,
                exception=None,
                retry_planned=False,
                runtime_status="crashed",
            )
            lifecycle.finalize(runtime_status="crashed")
            raise ProductionDriverError(
                "COLLECTOR_TASK_START_FAILED", "Collector task event is unavailable"
            )
        state.observation = initial_observation
        state.lifecycle = lifecycle
        state.task_binding = binding
        state.agent = agent
        state.policy = policy
        state.runtime_audit = runtime_audit

    def _step_context(
        self,
        state: _ProductionUnitStateV1,
        *,
        step_index: int,
        observation: Observation,
    ) -> AuditContext:
        binding = state.task_binding
        lifecycle = state.lifecycle
        if type(binding) is not TaskAuditBinding or type(lifecycle) is not AuditLifecycle:
            raise ProductionDriverError("COLLECTOR_TASK_BINDING_FAILED", "task runtime is absent")
        screenshot_bytes = _pil_png_bytes(observation.screenshot)
        step = binding.capture.start_step(
            step_index=step_index,
            observation=observation.model_dump(),
            source_screenshot_bytes=screenshot_bytes,
        )
        if (
            step is None
            or step.step_started_event_id is None
            or not binding.capture.capture_complete
        ):
            raise ProductionDriverError(
                "COLLECTOR_STEP_START_FAILED", "Collector current observation is unavailable"
            )
        return AuditContext(
            run_id=lifecycle.run_id,
            recorder=binding.task_recorder,
            task_run_id=binding.metadata.task_run_id,
            step_id=step.step_id,
            decision_id=step.decision_id,
            store_stream_chunks=binding.store_stream_chunks,
            model_call_trace=ModelCallTrace(),
            known_secrets=(),
            parent_event_id=step.step_started_event_id,
        )

    @staticmethod
    @contextmanager
    def _bound_logical_call(
        state: _ProductionUnitStateV1,
        context: AuditContext,
        *,
        actor_call_index: int,
        expected_actor_request_sha256: str | None,
    ) -> Iterator[SentinelLogicalCall]:
        agent = state.agent
        binding = state.task_binding
        if agent is None or type(binding) is not TaskAuditBinding:
            raise ProductionDriverError("ACTOR_RUNTIME_UNAVAILABLE", "actor runtime is absent")
        sentinel = getattr(agent, "_prompt_sentinel", None)
        if type(sentinel) is not PromptSentinel:
            raise ProductionDriverError("SENTINEL_RUNTIME_UNAVAILABLE", "PromptSentinel is absent")
        policy = state.policy
        if policy is None:
            yield sentinel.logical_call(
                host_id=cast(str, agent._sentinel_host_id),
                history_codec_id=cast(str, agent._sentinel_history_codec_id),
                call_role=SentinelCallRole.ACTOR,
                attributes={
                    "adapter": "production",
                    "r24_actor_call_index": actor_call_index,
                    "r24_case_deadline_monotonic_ns": state.deadline_monotonic_ns,
                },
            )
            return
        with policy.bind_case_task_call(
            binding,
            actor_call_index=actor_call_index,
            expected_actor_request_sha256=expected_actor_request_sha256,
        ):
            yield sentinel.logical_call(
                host_id=cast(str, agent._sentinel_host_id),
                history_codec_id=cast(str, agent._sentinel_history_codec_id),
                call_role=SentinelCallRole.ACTOR,
                attributes=policy.current_case_context_attributes(),
            )

    @staticmethod
    def _attempt_journal_for_call(
        state: _ProductionUnitStateV1,
        logical_call_id: str,
    ) -> tuple[list[JsonValue], list[str], str | None]:
        if state.policy is None:
            return [], [], None
        try:
            attempts = state.policy.attempt_receipts_for_call(logical_call_id)
        except Exception as exc:
            return [], [], _exception_code(exc, "ATTEMPT_JOURNAL_UNAVAILABLE")
        return (
            [cast(JsonValue, live_attempt_receipt_projection(item)) for item in attempts],
            [live_attempt_receipt_sha256(item) for item in attempts],
            None,
        )

    def _journal_latest_admission_failure_terminal(self, state: _ProductionUnitStateV1) -> None:
        audit = state.runtime_audit
        if audit is None:
            return
        receipt = getattr(audit, "latest_admission_failure_receipt", None)
        if type(receipt) is not ProductionRuntimeAuditAdmissionFailureReceiptV1:
            return
        receipt_sha256 = production_runtime_audit_admission_failure_receipt_sha256(receipt)
        if receipt_sha256 in state.terminal_audit_sha256s:
            return
        attempts, attempt_sha256s, attempt_failure = self._attempt_journal_for_call(
            state, receipt.logical_call_id
        )
        entry: dict[str, JsonValue] = {
            "attempt_journal_failure_code": attempt_failure,
            "kind": "ADMISSION_OUTCOME_UNKNOWN",
            "live_attempt_receipt_sha256s": cast(JsonValue, attempt_sha256s),
            "live_attempt_receipts": cast(JsonValue, attempts),
            "receipt": production_runtime_audit_admission_failure_receipt_projection(receipt),
            "receipt_sha256": receipt_sha256,
        }
        entry["canonical_evidence_sha256"] = _hash_projection(
            "production-unit-terminal-audit", cast(JsonValue, entry)
        )
        state.terminal_audit_sha256s.add(receipt_sha256)
        state.terminal_audit_journal.append(entry)

    def _journal_completed_audit_terminal(
        self,
        state: _ProductionUnitStateV1,
        receipt: ProductionRuntimeAuditReceiptV1,
    ) -> None:
        receipt_sha256 = production_runtime_audit_receipt_sha256(receipt)
        if receipt_sha256 in state.terminal_audit_sha256s:
            return
        attempts, attempt_sha256s, attempt_failure = self._attempt_journal_for_call(
            state, receipt.logical_call_id
        )
        entry: dict[str, JsonValue] = {
            "attempt_journal_failure_code": attempt_failure,
            "kind": "COMPLETED",
            "live_attempt_receipt_sha256s": cast(JsonValue, attempt_sha256s),
            "live_attempt_receipts": cast(JsonValue, attempts),
            "receipt": production_runtime_audit_receipt_projection(receipt),
            "receipt_sha256": receipt_sha256,
        }
        entry["canonical_evidence_sha256"] = _hash_projection(
            "production-unit-terminal-audit", cast(JsonValue, entry)
        )
        state.terminal_audit_sha256s.add(receipt_sha256)
        state.terminal_audit_journal.append(entry)

    def _journal_latest_failure_terminal(self, state: _ProductionUnitStateV1) -> None:
        audit = state.runtime_audit
        if audit is None:
            return
        receipt = audit.latest_failure_receipt
        if type(receipt) is not ProductionRuntimeAuditFailureReceiptV1:
            return
        receipt_sha256 = production_runtime_audit_failure_receipt_sha256(receipt)
        if receipt_sha256 in state.terminal_audit_sha256s:
            return
        attempts, attempt_sha256s, attempt_failure = self._attempt_journal_for_call(
            state, receipt.logical_call_id
        )
        entry: dict[str, JsonValue] = {
            "attempt_journal_failure_code": attempt_failure,
            "kind": "FAILED",
            "live_attempt_receipt_sha256s": cast(JsonValue, attempt_sha256s),
            "live_attempt_receipts": cast(JsonValue, attempts),
            "receipt": production_runtime_audit_failure_receipt_projection(receipt),
            "receipt_sha256": receipt_sha256,
        }
        entry["canonical_evidence_sha256"] = _hash_projection(
            "production-unit-terminal-audit", cast(JsonValue, entry)
        )
        state.terminal_audit_sha256s.add(receipt_sha256)
        state.terminal_audit_journal.append(entry)

    def _journal_latest_commit_failure_terminal(self, state: _ProductionUnitStateV1) -> None:
        audit = state.runtime_audit
        if audit is None:
            return
        receipt = audit.latest_commit_failure_receipt
        if type(receipt) is not ProductionRuntimeAuditCommitFailureReceiptV1:
            return
        receipt_sha256 = production_runtime_audit_commit_failure_receipt_sha256(receipt)
        if receipt_sha256 in state.terminal_audit_sha256s:
            return
        attempts, attempt_sha256s, attempt_failure = self._attempt_journal_for_call(
            state, receipt.logical_call_id
        )
        entry: dict[str, JsonValue] = {
            "attempt_journal_failure_code": attempt_failure,
            "kind": "COMMIT_OUTCOME_UNKNOWN",
            "live_attempt_receipt_sha256s": cast(JsonValue, attempt_sha256s),
            "live_attempt_receipts": cast(JsonValue, attempts),
            "receipt": production_runtime_audit_commit_failure_receipt_projection(receipt),
            "receipt_sha256": receipt_sha256,
        }
        entry["canonical_evidence_sha256"] = _hash_projection(
            "production-unit-terminal-audit", cast(JsonValue, entry)
        )
        state.terminal_audit_sha256s.add(receipt_sha256)
        state.terminal_audit_journal.append(entry)

    def _journal_latest_audit_terminals(self, state: _ProductionUnitStateV1) -> None:
        audit = state.runtime_audit
        if audit is None:
            return
        self._journal_latest_admission_failure_terminal(state)
        completed = audit.latest_completed_receipt
        if type(completed) is ProductionRuntimeAuditReceiptV1:
            self._journal_completed_audit_terminal(state, completed)
        self._journal_latest_failure_terminal(state)
        self._journal_latest_commit_failure_terminal(state)

    @staticmethod
    def _unit_evidence_blob_name(content_sha256: str) -> str:
        if _SHA256.fullmatch(content_sha256) is None:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_REFERENCE_INVALID",
                "unit evidence content digest is invalid",
            )
        return f"{content_sha256}{_UNIT_EVIDENCE_BLOB_SUFFIX}"

    @staticmethod
    def _unit_evidence_blob_reference(*, unit_id: str, raw: bytes) -> dict[str, JsonValue]:
        content_sha256 = hashlib.sha256(raw).hexdigest()
        projection: dict[str, JsonValue] = {
            "blob": {
                "algorithm": "sha256",
                "byte_count": len(raw),
                "safe_locator": (
                    _ProductionFixedExecutionPortV1._unit_evidence_blob_name(content_sha256)
                ),
                "sha256": content_sha256,
            },
            "schema_version": _UNIT_EVIDENCE_BLOB_REFERENCE_SCHEMA_VERSION,
            "storage": _UNIT_EVIDENCE_BLOB_STORAGE_KIND,
            "unit_id": unit_id,
        }
        projection["reference_sha256"] = canonical_sha256(cast(JsonValue, projection))
        return projection

    def _open_unit_evidence_blob_root(self) -> int:
        sink = getattr(self, "_audit_sink", None)
        if type(sink) is not ExternalProductionRuntimeAuditSinkV1:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_SINK_UNAVAILABLE",
                "the exact owner-only production audit root is unavailable",
            )
        try:
            return sink._open_root()
        except Exception as exc:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_ROOT_INVALID",
                "the owner-only production audit root failed re-attestation",
            ) from exc

    def _read_unit_evidence_blob(
        self,
        *,
        directory_fd: int,
        safe_locator: str,
        expected_byte_count: int,
        expected_sha256: str,
        missing_ok: bool = False,
    ) -> bytes | None:
        if (
            type(safe_locator) is not str
            or safe_locator != self._unit_evidence_blob_name(expected_sha256)
            or type(expected_byte_count) is not int
            or expected_byte_count <= _MAX_INLINE_UNIT_EVIDENCE_JOURNAL_BYTES
        ):
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_REFERENCE_INVALID",
                "unit evidence blob locator or size differs",
            )
        sink = self._audit_sink
        descriptor = -1
        try:
            sink._validate_open_root(directory_fd)
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(safe_locator, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise ProductionDriverError(
                    "UNIT_EVIDENCE_BLOB_MISSING",
                    "referenced unit evidence blob is absent",
                ) from None
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_gid != os.getegid()
                or info.st_nlink != 1
                or info.st_size != expected_byte_count
            ):
                raise ProductionDriverError(
                    "UNIT_EVIDENCE_BLOB_INVALID",
                    "unit evidence blob metadata differs",
                )
            chunks: list[bytes] = []
            bytes_read = 0
            while True:
                chunk = os.read(descriptor, min(1_048_576, expected_byte_count - bytes_read + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                bytes_read += len(chunk)
                if bytes_read > expected_byte_count:
                    raise ProductionDriverError(
                        "UNIT_EVIDENCE_BLOB_INVALID",
                        "unit evidence blob grew during readback",
                    )
            raw = b"".join(chunks)
            if (
                len(raw) != expected_byte_count
                or hashlib.sha256(raw).hexdigest() != expected_sha256
            ):
                raise ProductionDriverError(
                    "UNIT_EVIDENCE_BLOB_INVALID",
                    "unit evidence blob content differs from its reference",
                )
            sink._validate_open_root(directory_fd)
            return raw
        except ProductionDriverError:
            raise
        except Exception as exc:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_READ_FAILED",
                "unit evidence blob could not be read safely",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _write_unit_evidence_blob(descriptor: int, raw: bytes) -> None:
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short unit evidence blob write")
            remaining = remaining[written:]

    def _persist_unit_evidence_blob(self, *, unit_id: str, raw: bytes) -> bytes:
        reference = self._unit_evidence_blob_reference(unit_id=unit_id, raw=raw)
        blob = cast(dict[str, JsonValue], reference["blob"])
        content_sha256 = cast(str, blob["sha256"])
        safe_locator = cast(str, blob["safe_locator"])
        byte_count = cast(int, blob["byte_count"])
        directory_fd = self._open_unit_evidence_blob_root()
        temporary = f".{content_sha256}.{secrets.token_hex(12)}.unit-evidence.tmp"
        temporary_fd = -1
        try:
            try:
                existing = self._read_unit_evidence_blob(
                    directory_fd=directory_fd,
                    safe_locator=safe_locator,
                    expected_byte_count=byte_count,
                    expected_sha256=content_sha256,
                    missing_ok=True,
                )
            except ProductionDriverError as exc:
                raise ProductionDriverError(
                    "UNIT_EVIDENCE_BLOB_COLLISION",
                    "an existing content-addressed unit evidence blob differs",
                ) from exc
            if existing is not None:
                os.fsync(directory_fd)
                return canonical_json_bytes(cast(JsonValue, reference))

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            temporary_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            self._write_unit_evidence_blob(temporary_fd, raw)
            os.fsync(temporary_fd)
            info = os.fstat(temporary_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_gid != os.getegid()
                or info.st_nlink != 1
                or info.st_size != byte_count
            ):
                raise OSError("unit evidence blob staging metadata changed")
            self._audit_sink._validate_open_root(directory_fd)
            try:
                # Link publication is atomic and, unlike replace/rename, cannot
                # overwrite a colliding content-addressed destination.
                os.link(
                    temporary,
                    safe_locator,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                try:
                    concurrent = self._read_unit_evidence_blob(
                        directory_fd=directory_fd,
                        safe_locator=safe_locator,
                        expected_byte_count=byte_count,
                        expected_sha256=content_sha256,
                    )
                except ProductionDriverError as exc:
                    raise ProductionDriverError(
                        "UNIT_EVIDENCE_BLOB_COLLISION",
                        "a concurrent content-addressed unit evidence blob differs",
                    ) from exc
                if concurrent != raw:
                    raise ProductionDriverError(
                        "UNIT_EVIDENCE_BLOB_COLLISION",
                        "a concurrent content-addressed unit evidence blob differs",
                    )
            os.unlink(temporary, dir_fd=directory_fd)
            temporary = ""
            os.fsync(directory_fd)
            published = self._read_unit_evidence_blob(
                directory_fd=directory_fd,
                safe_locator=safe_locator,
                expected_byte_count=byte_count,
                expected_sha256=content_sha256,
            )
            if published != raw:
                raise ProductionDriverError(
                    "UNIT_EVIDENCE_BLOB_INVALID",
                    "published unit evidence blob differs on exact readback",
                )
            return canonical_json_bytes(cast(JsonValue, reference))
        except ProductionDriverError:
            raise
        except Exception as exc:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_PERSIST_FAILED",
                "unit evidence blob could not be durably published",
            ) from exc
        finally:
            if temporary_fd >= 0:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)

    def _decode_unit_journal_snapshot(
        self, journal_raw: bytes, *, expected_unit_id: str
    ) -> dict[str, JsonValue]:
        if type(journal_raw) is not bytes or not journal_raw:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_JOURNAL_INVALID",
                "sealed per-unit terminal evidence is absent",
            )
        if len(journal_raw) > _MAX_INLINE_UNIT_EVIDENCE_JOURNAL_BYTES:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_JOURNAL_TOO_LARGE",
                "inline per-unit terminal evidence exceeds the sealed bound",
            )
        try:
            decoded = json.loads(journal_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_JOURNAL_INVALID",
                "sealed per-unit terminal evidence is invalid",
            ) from exc
        if type(decoded) is not dict:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_JOURNAL_INVALID",
                "sealed per-unit terminal evidence is not an object",
            )
        projection = cast(dict[str, JsonValue], decoded)
        if projection.get("schema_version") != _UNIT_EVIDENCE_BLOB_REFERENCE_SCHEMA_VERSION:
            return self._decode_full_unit_journal_raw(
                journal_raw, expected_unit_id=expected_unit_id
            )
        if (
            set(projection) != {"blob", "reference_sha256", "schema_version", "storage", "unit_id"}
            or projection.get("storage") != _UNIT_EVIDENCE_BLOB_STORAGE_KIND
            or projection.get("unit_id") != expected_unit_id
            or type(projection.get("reference_sha256")) is not str
            or canonical_json_bytes(cast(JsonValue, projection)) != journal_raw
        ):
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_REFERENCE_INVALID",
                "unit evidence blob reference differs",
            )
        reference_preimage = dict(projection)
        claimed_reference_sha256 = reference_preimage.pop("reference_sha256")
        if canonical_sha256(cast(JsonValue, reference_preimage)) != claimed_reference_sha256:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_REFERENCE_INVALID",
                "unit evidence blob reference hash differs",
            )
        blob_value = projection.get("blob")
        if type(blob_value) is not dict or set(blob_value) != {
            "algorithm",
            "byte_count",
            "safe_locator",
            "sha256",
        }:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_REFERENCE_INVALID",
                "unit evidence blob binding differs",
            )
        blob = blob_value
        algorithm = blob.get("algorithm")
        byte_count = blob.get("byte_count")
        safe_locator = blob.get("safe_locator")
        content_sha256 = blob.get("sha256")
        if (
            algorithm != "sha256"
            or type(byte_count) is not int
            or type(safe_locator) is not str
            or type(content_sha256) is not str
        ):
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_REFERENCE_INVALID",
                "unit evidence blob binding types differ",
            )
        directory_fd = self._open_unit_evidence_blob_root()
        try:
            blob_raw = self._read_unit_evidence_blob(
                directory_fd=directory_fd,
                safe_locator=safe_locator,
                expected_byte_count=byte_count,
                expected_sha256=content_sha256,
            )
        finally:
            os.close(directory_fd)
        if blob_raw is None:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_MISSING",
                "referenced unit evidence blob is absent",
            )
        return self._decode_full_unit_journal_raw(blob_raw, expected_unit_id=expected_unit_id)

    def _validated_smoke_unit_journal_reference(
        self, journal_raw: bytes, *, expected_unit_id: str
    ) -> _ValidatedUnitJournalReferenceV1 | None:
        """Resolve a smoke journal and seal exact owner-only blob readback evidence."""

        full = self._decode_unit_journal_snapshot(journal_raw, expected_unit_id=expected_unit_id)
        dispatches = full.get("resource_dispatch_records")
        dispatches_sha256 = full.get("resource_dispatch_records_sha256")
        if (
            type(dispatches) is not list
            or type(dispatches_sha256) is not str
            or dispatches_sha256
            != _hash_projection(
                "production-unit-resource-dispatch-journal",
                cast(JsonValue, dispatches),
            )
        ):
            raise ProductionDriverError(
                "INVALID_UNIT_JOURNAL",
                "smoke unit journal lacks its dispatch census",
            )
        decoded = cast(dict[str, JsonValue], json.loads(journal_raw))
        if decoded.get("schema_version") != _UNIT_EVIDENCE_BLOB_REFERENCE_SCHEMA_VERSION:
            return None
        blob = cast(dict[str, JsonValue], decoded["blob"])
        sink = getattr(self, "_audit_sink", None)
        if type(sink) is not ExternalProductionRuntimeAuditSinkV1:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_BLOB_SINK_UNAVAILABLE",
                "validated journal reference lacks its exact owner-only root",
            )
        directory_fd = self._open_unit_evidence_blob_root()
        try:
            info = os.fstat(directory_fd)
            root_identity_sha256 = _hash_projection(
                "production-unit-evidence-owner-root-identity",
                cast(
                    JsonValue,
                    {
                        "canonical_path_sha256": hashlib.sha256(os.fsencode(sink.root)).hexdigest(),
                        "device": info.st_dev,
                        "gid": info.st_gid,
                        "inode": info.st_ino,
                        "uid": info.st_uid,
                    },
                ),
            )
        finally:
            os.close(directory_fd)
        return _ValidatedUnitJournalReferenceV1(
            expected_unit_id=expected_unit_id,
            reference_preimage_sha256=hashlib.sha256(journal_raw).hexdigest(),
            reference_sha256=cast(str, decoded["reference_sha256"]),
            blob_sha256=cast(str, blob["sha256"]),
            blob_byte_count=cast(int, blob["byte_count"]),
            blob_safe_locator=cast(str, blob["safe_locator"]),
            owner_audit_root_identity_sha256=root_identity_sha256,
            resource_dispatch_records_sha256=dispatches_sha256,
            resource_dispatch_record_count=len(dispatches),
            _resolver=self,
            _seal=_MODULE_SEAL,
        )

    @staticmethod
    def _decode_full_unit_journal_raw(raw: bytes, *, expected_unit_id: str) -> dict[str, JsonValue]:
        """Validate a complete canonical in-memory or blob-backed journal."""

        if type(raw) is not bytes or not raw:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_JOURNAL_INVALID",
                "full per-unit terminal evidence is absent",
            )
        try:
            restored = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_JOURNAL_INVALID",
                "full per-unit terminal evidence is not valid JSON",
            ) from exc
        if (
            type(restored) is not dict
            or restored.get("unit_id") != expected_unit_id
            or canonical_json_bytes(cast(JsonValue, restored)) != raw
        ):
            raise ProductionDriverError(
                "UNIT_EVIDENCE_JOURNAL_INVALID",
                "full per-unit terminal evidence is not its canonical unit projection",
            )
        return cast(dict[str, JsonValue], restored)

    def _unit_journal_full_snapshot(self, state: _ProductionUnitStateV1) -> bytes:
        """Build the complete canonical unit projection without external I/O."""

        self._journal_latest_audit_terminals(state)
        decisions = [_decision_projection(item) for item in state.decision_journal]
        terminals = [dict(item) for item in state.terminal_audit_journal]
        fatal_state = self._run_fatal_latch.state
        cleanup_recovery_outcome = (
            None if state.cleanup_recovery_outcome is None else dict(state.cleanup_recovery_outcome)
        )
        resource_dispatches = [dict(item) for item in state.resource_dispatch_journal]
        raw = canonical_json_bytes(
            cast(
                JsonValue,
                {
                    "cleanup_recovery_outcome": cleanup_recovery_outcome,
                    "cleanup_recovery_outcome_sha256": (
                        None
                        if cleanup_recovery_outcome is None
                        else _hash_projection(
                            "production-unit-cleanup-recovery-outcome",
                            cast(JsonValue, cleanup_recovery_outcome),
                        )
                    ),
                    "completed_decisions": decisions,
                    "completed_decisions_sha256": _hash_projection(
                        "production-unit-decision-journal", cast(JsonValue, decisions)
                    ),
                    "terminal_audit_records": terminals,
                    "terminal_audit_records_sha256": _hash_projection(
                        "production-unit-terminal-audit-journal",
                        cast(JsonValue, terminals),
                    ),
                    "run_fatal_state": (
                        None
                        if fatal_state is None
                        else production_run_fatal_state_projection(fatal_state)
                    ),
                    "run_fatal_state_sha256": (
                        None
                        if fatal_state is None
                        else production_run_fatal_state_sha256(fatal_state)
                    ),
                    "resource_dispatch_records": resource_dispatches,
                    "resource_dispatch_records_sha256": _hash_projection(
                        "production-unit-resource-dispatch-journal",
                        cast(JsonValue, resource_dispatches),
                    ),
                    "unit_deadline": cast(
                        JsonValue,
                        _deadline_projection(
                            execution_deadline=state.deadline_monotonic_ns,
                            cleanup_deadline=state.cleanup_deadline_monotonic_ns,
                            authority_deadline=state.authority_deadline_monotonic_ns,
                            attempt_termination_upper_bound_ns=(
                                state.attempt_termination_upper_bound_ns
                            ),
                        ),
                    ),
                    "unit_id": state.unit_id,
                },
            )
        )
        return raw

    def _retain_unpublished_unit_evidence(
        self,
        *,
        state: _ProductionUnitStateV1,
        raw: bytes,
        publication_failure_code: str,
        durably_republished: bool = False,
    ) -> _UnpublishedUnitEvidenceV1:
        recovery = _UnpublishedUnitEvidenceV1(
            unit_id=state.unit_id,
            raw=raw,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            byte_count=len(raw),
            publication_failure_code=publication_failure_code,
            durably_republished=durably_republished,
        )
        with self._lock:
            self._unpublished_unit_evidence[state.unit_id] = recovery
        return recovery

    def _unit_journal_snapshot(self, state: _ProductionUnitStateV1) -> bytes:
        raw = self._unit_journal_full_snapshot(state)
        if len(raw) <= _MAX_INLINE_UNIT_EVIDENCE_JOURNAL_BYTES:
            return raw
        try:
            reference = self._persist_unit_evidence_blob(unit_id=state.unit_id, raw=raw)
        except Exception as exc:
            prior_recovery = self._unpublished_unit_evidence_for(state.unit_id)
            self._retain_unpublished_unit_evidence(
                state=state,
                raw=raw,
                publication_failure_code=(
                    prior_recovery.publication_failure_code
                    if prior_recovery is not None
                    else _exception_code(exc, "UNIT_EVIDENCE_PUBLICATION_FAILED")
                ),
            )
            raise
        prior_recovery = self._unpublished_unit_evidence_for(state.unit_id)
        if prior_recovery is not None:
            # A retry may run after cleanup has enriched the unit projection.
            # Bind the exact newly published projection while retaining the
            # original typed publication fault for the outer failure record.
            self._retain_unpublished_unit_evidence(
                state=state,
                raw=raw,
                publication_failure_code=prior_recovery.publication_failure_code,
                durably_republished=True,
            )
        return reference

    def _receipt_for_action(
        self,
        state: _ProductionUnitStateV1,
        action: JSONAction,
        *,
        action_executed: bool,
        action_execution_ns: int,
    ) -> ProductionRuntimeAuditReceiptV1:
        agent = state.agent
        if agent is None:
            raise ProductionDriverError("ACTOR_RUNTIME_UNAVAILABLE", "actor is absent")
        try:
            receipt = agent.finalize_prompt_sentinel_action_execution(
                action=action,
                action_executed=action_executed,
                action_execution_ns=action_execution_ns,
            )
        except Exception:
            self._journal_latest_audit_terminals(state)
            raise
        if type(receipt) is not ProductionRuntimeAuditReceiptV1 or (
            production_runtime_audit_receipt_sha256(receipt) == ""
        ):
            raise ProductionDriverError(
                "PRODUCTION_AUDIT_RECEIPT_MISSING", "actor terminal audit is unavailable"
            )
        self._journal_completed_audit_terminal(state, receipt)
        return receipt

    def _decision_from_receipt(
        self,
        state: _ProductionUnitStateV1,
        receipt: ProductionRuntimeAuditReceiptV1,
        *,
        actor_call_index: int,
    ) -> ActorDecisionEvidenceV1:
        if not receipt.live_cost_exact:
            raise ProductionDriverError(
                "LIVE_COST_ACCOUNTING_UNKNOWN",
                "post-dispatch unknown cost cannot enter a successful stage decision",
            )
        policy = state.policy
        if policy is None:
            rubric_hashes: tuple[str, ...] = ()
            history_hash = None
            lease_hash = None
            authority_hash = None
            rubric_dispatches = 0
            history_dispatches = 0
            if receipt.live_openai_calls != 0 or receipt.live_cost_usd_micros != 0:
                raise ProductionDriverError(
                    "LIVE_CALL_BINDING_MISMATCH", "OFF receipt contains semantic usage"
                )
        else:
            try:
                attempts = policy.attempt_receipts_for_call(receipt.logical_call_id)
            except Exception:
                attempts = ()
            if any(
                item.logical_call_id != receipt.logical_call_id
                or item.actor_request_sha256 != receipt.raw_request_sha256
                for item in attempts
            ):
                raise ProductionDriverError(
                    "LIVE_CALL_BINDING_MISMATCH", "terminal attempt evidence differs"
                )
            rubric_attempts = tuple(
                item for item in attempts if item.role is LiveAttemptRoleV1.RUBRIC
            )
            history_attempts = tuple(
                item for item in attempts if item.role is LiveAttemptRoleV1.HISTORY_POLICY
            )
            if len(history_attempts) > 1:
                raise ProductionDriverError(
                    "LIVE_CALL_BINDING_MISMATCH", "history attempt cardinality differs"
                )
            rubric_hashes = tuple(live_attempt_receipt_sha256(item) for item in rubric_attempts)
            history_hash = (
                None if not history_attempts else live_attempt_receipt_sha256(history_attempts[0])
            )
            rubric_dispatches = sum(item.dispatch_count for item in rubric_attempts)
            history_dispatches = sum(item.dispatch_count for item in history_attempts)
            lease_hash = None if not attempts else attempts[0].case_execution_lease_sha256
            authority_hash = None if not attempts else policy.execution_authority_sha256
            if receipt.live_openai_calls != rubric_dispatches + history_dispatches:
                raise ProductionDriverError(
                    "LIVE_CALL_BINDING_MISMATCH", "terminal attempt dispatch census differs"
                )
            try:
                binding = policy.call_binding(receipt.logical_call_id)
            except Exception:
                binding = None
            if binding is not None:
                if binding.actor_call_index != actor_call_index:
                    raise ProductionDriverError(
                        "LIVE_CALL_BINDING_MISMATCH", "actor call index differs"
                    )
                if (
                    binding.actor_request_sha256 != receipt.raw_request_sha256
                    or binding.openai_calls != receipt.live_openai_calls
                    or binding.cost_usd_micros != receipt.live_cost_usd_micros
                    or binding.rubric_attempt_receipt_sha256s != rubric_hashes
                    or binding.history_policy_attempt_receipt_sha256 != history_hash
                    or binding.case_execution_lease_sha256 != lease_hash
                    or binding.execution_authority_sha256 != authority_hash
                ):
                    raise ProductionDriverError(
                        "LIVE_CALL_BINDING_MISMATCH", "audit/live policy evidence differs"
                    )
        decision = ActorDecisionEvidenceV1(
            logical_call_id=receipt.logical_call_id,
            actor_call_index=actor_call_index,
            raw_request_sha256=receipt.raw_request_sha256,
            final_request_sha256=receipt.final_request_sha256,
            provider_request_sha256=receipt.provider_request_sha256,
            provider_response_sha256=receipt.provider_response_sha256,
            exact_diff_sha256=receipt.exact_diff_sha256,
            pre_provider_status=receipt.pre_provider_status,
            pre_provider_outcome=receipt.pre_provider_outcome,
            fallback_reason=receipt.fallback_reason,
            fallback_check=receipt.fallback_check,
            preflight_report_sha256=self._factory.preflight_report_sha256,
            case_execution_lease_sha256=lease_hash,
            live_policy_factory_binding_sha256=self._factory.factory_binding_sha256,
            live_policy_authority_sha256=authority_hash,
            rubric_attempt_receipt_sha256s=rubric_hashes,
            history_policy_attempt_receipt_sha256=history_hash,
            actor_attempt_receipt_sha256=receipt.actor_provider_attempt_root_sha256,
            sentinel_receipt_sha256=receipt.sentinel_receipt_sha256,
            provider_attempt_receipt_sha256=receipt.actor_provider_attempt_root_sha256,
            runtime_audit_detail_sha256=receipt.detail_sha256,
            parser_result_sha256=receipt.parser_result_sha256,
            parsed_action_sha256=receipt.parsed_action_sha256,
            executed_action_sha256=receipt.executed_action_sha256,
            census=DriverCallCensusV1(
                actor_calls=1,
                offline_rubric_evaluations=0,
                rubric_openai_calls=rubric_dispatches,
                history_policy_openai_calls=history_dispatches,
                openai_calls=receipt.live_openai_calls,
                actor_actions=1 if receipt.action_executed else 0,
                cost_usd_micros=receipt.live_cost_usd_micros,
                wall_time_ms=(receipt.total_ns + 999_999) // 1_000_000,
            ),
        )
        if type(state) is _ProductionUnitStateV1:
            state.decision_journal.append(decision)
        if (
            type(state) is _ProductionUnitStateV1
            and policy is not None
            and not _semantic_pre_provider_outcome_admitted(decision)
        ):
            raise ProductionDriverError(
                "SENTINEL_PRE_PROVIDER_OUTCOME_REJECTED",
                "semantic actor used a non-admissible pre-provider fallback",
            )
        return decision

    def _record_decision(
        self,
        state: _ProductionUnitStateV1,
        context: AuditContext,
        prediction: str,
        action: JSONAction,
    ) -> object:
        binding = state.task_binding
        if type(binding) is not TaskAuditBinding:
            raise ProductionDriverError("COLLECTOR_TASK_BINDING_FAILED", "task binding is absent")
        decision = binding.capture.record_decision(
            prediction=prediction,
            action=action,
            model_call_trace=context.model_call_trace,
        )
        if decision is None or decision.event_id is None or not binding.capture.capture_complete:
            raise ProductionDriverError(
                "COLLECTOR_DECISION_FAILED", "Collector decision evidence is unavailable"
            )
        return decision

    def _dispatch_smoke_fixture(
        self,
        state: _ProductionUnitStateV1,
        fixture: _LoadedSmokeFixtureV1,
    ) -> tuple[str, JSONAction, ActorDecisionEvidenceV1]:
        agent = state.agent
        observation = state.observation
        if agent is None or observation is None:
            raise ProductionDriverError("ACTOR_RUNTIME_UNAVAILABLE", "smoke actor is absent")
        request = fixture.request
        model = request.get("model")
        messages = request.get("messages")
        if (
            type(model) is not str
            or type(messages) is not list
            or model != self._resources[state.host].served_model_id
        ):
            raise ProductionDriverError("SMOKE_FIXTURE_MISMATCH", "fixture actor binding differs")
        kwargs = {key: value for key, value in request.items() if key not in {"model", "messages"}}
        if kwargs.pop("stream", False) is not False:
            raise ProductionDriverError("SMOKE_FIXTURE_MISMATCH", "streaming smoke is unsupported")
        context = self._step_context(state, step_index=1, observation=observation)
        with bind_audit_context(context):
            with self._bound_logical_call(
                state,
                context,
                actor_call_index=1,
                expected_actor_request_sha256=fixture.request_sha256,
            ) as call:
                self._require_resource_dispatch(
                    state.host,
                    ProductionDispatchKindV1.ACTOR,
                    deadline_ns=state.deadline_monotonic_ns,
                    state=state,
                )
                with bind_sentinel_logical_call(call):
                    prediction = agent.openai_chat_completions_create(
                        model=model,
                        messages=cast(list[dict], messages),
                        retry_times=1,
                        **cast(dict[str, Any], kwargs),
                    )
                if type(prediction) is not str:
                    agent._finalize_prompt_sentinel_actor_failure(
                        call,
                        failure_phase="ACTOR_PROVIDER",
                        failure_code="ACTOR_PROVIDER_FAILED",
                    )
                    self._journal_latest_failure_terminal(state)
                    raise ProductionDriverError(
                        "ACTOR_PROVIDER_FAILED", "smoke provider returned no text"
                    )
                parser_started = time.monotonic_ns()
                try:
                    if type(agent) is Qwen3VLAgentMCP:
                        parsed = parse_qwen_action(prediction)
                        if parsed["action_name"] == "mobile_use":
                            image = cast(Image.Image, observation.screenshot)
                            action = JSONAction(
                                **parsing_response_to_andoid_world_env_action(
                                    parsed, image.height, image.width
                                )
                            )
                        else:
                            action = JSONAction(
                                action_type=MCP,
                                action_name=parsed["action_name"],
                                action_json=parsed["action_json"],
                            )
                        parser_id = "mobileworld.qwen3vl.action-parser.v1"
                    elif type(agent) is MAIUINaivigationAgent:
                        parsed = parse_mai_action(prediction)
                        action = agent._convert_to_json_action(
                            parsed.get("tool_name", "mobile_use"),
                            parsed["action_json"],
                            observation.screenshot,
                        )
                        parser_id = "mobileworld.mai-ui.action-parser.v1"
                    else:
                        raise ProductionDriverError(
                            "ACTOR_RUNTIME_UNAVAILABLE", "host type differs"
                        )
                except Exception:
                    agent._finalize_prompt_sentinel_actor_failure(
                        call,
                        failure_phase="ACTOR_PARSER",
                        failure_code="ACTOR_PARSER_FAILED",
                    )
                    self._journal_latest_failure_terminal(state)
                    raise
                parser_ns = max(0, time.monotonic_ns() - parser_started)
                agent._finalize_prompt_sentinel_actor_output(
                    call,
                    prediction=prediction,
                    action=action,
                    parser_id=parser_id,
                    parser_succeeded=True,
                    parser_attempt_count=1,
                    parser_ns=parser_ns,
                )
                decision = self._record_decision(state, context, prediction, action)
                assert state.task_binding is not None
                state.task_binding.capture.transition_not_executed(
                    reason="R2.4 live smoke forbids GUI actions",
                    decision=decision,
                )
                receipt = self._receipt_for_action(
                    state,
                    action,
                    action_executed=False,
                    action_execution_ns=0,
                )
                decision_evidence = self._decision_from_receipt(
                    state,
                    receipt,
                    actor_call_index=1,
                )
                if receipt.raw_request_sha256 != fixture.request_sha256:
                    raise ProductionDriverError(
                        "SMOKE_FIXTURE_MISMATCH", "provider path did not use exact fixture request"
                    )
                return prediction, action, decision_evidence

    def prepare_pilot(self, pilot: FrozenPilotManifestV1) -> None:
        self._require_pilot_scope_authorized()
        with self._lock:
            if self._pilot_inputs is not None:
                raise ProductionDriverError("PILOT_ALREADY_PREPARED", "pilot inputs repeat")
            resolved = resolve_pilot_task_inputs_v1(
                pilot,
                authorized_input_root=self._config.authorized_pilot_input_root,
                repository_root=self._config.repository_root,
            )
            declared = {item.task_id: item for item in pilot.tasks}
            if any(
                item.task_id not in declared
                or declared[item.task_id].task_parameters_sha256 != item.task_parameters_sha256
                or declared[item.task_id].reset_seed != item.reset_seed
                for item in resolved.tasks
            ):
                raise ProductionDriverError(
                    "PILOT_TASK_INPUT_MISMATCH", "resolved pilot task authority differs"
                )
            resolved_pilot_task_inputs_sha256(resolved)
            self._pilot_inputs = resolved

    def reset_pilot_cell(self, invocation: _PilotInvocationV1) -> _PilotResetResultV1:
        self._require_pilot_scope_authorized()
        self._require_invocation_deadlines(invocation)
        self._require_run_dispatch_allowed()
        unit_id = self._unit_id(invocation)
        with self._lock:
            resolved = self._pilot_inputs
            if resolved is None:
                raise ProductionDriverError(
                    "PILOT_INPUTS_UNRESOLVED", "pilot inputs must resolve before reset"
                )
            task_input = next(
                (item for item in resolved.tasks if item.task_id == invocation.cell.task_id), None
            )
            if (
                task_input is None
                or task_input.task_parameters_sha256 != invocation.cell.task_parameters_sha256
                or task_input.reset_seed != invocation.cell.reset_seed
            ):
                raise ProductionDriverError(
                    "PILOT_TASK_INPUT_MISMATCH", "cell task binding differs before reset"
                )
            if unit_id in self._units:
                raise ProductionDriverError("UNIT_ALREADY_STARTED", "pilot cell repeats")
            environment = AndroidEnvClient(
                url=f"http://127.0.0.1:{self._config.backend_port}",
                device=self._config.backend_device,
                step_wait_time=1.0,
                trust_env=False,
                request_deadline_monotonic_ns=invocation.deadline_monotonic_ns,
            )
            state = _ProductionUnitStateV1(
                unit_id=unit_id,
                host=invocation.cell.host,
                task_name=task_input.task_name,
                deadline_monotonic_ns=invocation.deadline_monotonic_ns,
                cleanup_deadline_monotonic_ns=invocation.cleanup_deadline_monotonic_ns,
                authority_deadline_monotonic_ns=invocation.authority_deadline_monotonic_ns,
                attempt_termination_upper_bound_ns=(invocation.attempt_termination_upper_bound_ns),
                environment=environment,
                observation=None,
                task_input=task_input,
            )
            self._units[unit_id] = state
        self._require_resource_dispatch(
            invocation.cell.host,
            ProductionDispatchKindV1.BACKEND_RESET,
            deadline_ns=invocation.deadline_monotonic_ns,
            state=state,
        )
        observation = environment.initialize_task(
            task_input.task_name,
            task_trial=task_input.trial,
            task_parameters_sha256=task_input.task_parameters_sha256,
            reset_seed=task_input.reset_seed,
        )
        if type(observation) is not Observation:
            raise ProductionDriverError("PILOT_RESET_FAILED", "task init result type differs")
        self._require_resource_dispatch(
            invocation.cell.host,
            ProductionDispatchKindV1.BACKEND_TASK_GOAL,
            deadline_ns=invocation.deadline_monotonic_ns,
            state=state,
        )
        task_goal = environment.get_task_goal(task_input.task_name)
        if type(task_goal) is not str or not task_goal:
            raise ProductionDriverError("PILOT_TASK_GOAL_MISSING", "task goal is unavailable")
        state.observation = observation
        state.task_goal = task_goal
        screenshot_sha256 = hashlib.sha256(_pil_png_bytes(observation.screenshot)).hexdigest()
        effective_reset_state_sha256 = _hash_projection(
            "production-pilot-effective-reset-state",
            cast(
                JsonValue,
                {
                    "observation_screenshot_sha256": screenshot_sha256,
                    "reset_seed": task_input.reset_seed,
                    "task_goal_sha256": hashlib.sha256(task_goal.encode("utf-8")).hexdigest(),
                    "task_id": task_input.task_id,
                    "task_name": task_input.task_name,
                    "task_parameters_sha256": task_input.task_parameters_sha256,
                    "trial": task_input.trial,
                },
            ),
        )
        return _PilotResetResultV1(
            reset_receipt_sha256=_hash_projection(
                "production-pilot-reset",
                cast(
                    JsonValue,
                    {
                        "backend_endpoint": f"http://127.0.0.1:{self._config.backend_port}",
                        "case_id": f"pilot-cell-{invocation.sequence_index:03d}",
                        "manifest_sha256": invocation.manifest_sha256,
                        "effective_reset_state_sha256": effective_reset_state_sha256,
                        "observation_screenshot_sha256": screenshot_sha256,
                        "resolved_inputs_sha256": resolved_pilot_task_inputs_sha256(resolved),
                        "task_id": task_input.task_id,
                        "task_name": task_input.task_name,
                        "task_parameters_sha256": task_input.task_parameters_sha256,
                        "trial": task_input.trial,
                        "reset_seed": task_input.reset_seed,
                    },
                ),
            ),
            effective_reset_state_sha256=effective_reset_state_sha256,
        )

    def run_smoke_case(
        self,
        invocation: _SmokeInvocationV1,
        lease: CaseAuthorityBrokerV1,
    ) -> _SmokePortResultV1:
        self._require_broker(lease)
        self._require_invocation_deadlines(invocation)
        self._require_run_dispatch_allowed()
        fixture = _read_hash_bound_smoke_fixture(
            invocation.case,
            host=invocation.host,
            authorized_input_root=Path(self._config.authorized_pilot_input_root),
            repository_root=Path(self._config.repository_root),
        )
        image = Image.open(io.BytesIO(fixture.current_image_png))
        image.load()
        observation = Observation(screenshot=image, accessibility_tree=None)
        unit_id = self._unit_id(invocation)
        state = _ProductionUnitStateV1(
            unit_id=unit_id,
            host=invocation.host,
            task_name=invocation.case.task_id,
            deadline_monotonic_ns=invocation.deadline_monotonic_ns,
            cleanup_deadline_monotonic_ns=invocation.cleanup_deadline_monotonic_ns,
            authority_deadline_monotonic_ns=invocation.authority_deadline_monotonic_ns,
            attempt_termination_upper_bound_ns=invocation.attempt_termination_upper_bound_ns,
            environment=None,
            observation=observation,
        )
        with self._lock:
            if unit_id in self._units:
                raise ProductionDriverError("UNIT_ALREADY_STARTED", "smoke case repeats")
            self._units[unit_id] = state
        self._begin_task_runtime(
            invocation=invocation,
            state=state,
            task_goal=fixture.task_instruction,
            initial_observation=observation,
        )
        _, _, decision = self._dispatch_smoke_fixture(state, fixture)
        state.final_step_index = 1
        state.completed = True
        return _SmokePortResultV1(
            request_fixture_sha256=invocation.case.request_fixture_sha256,
            request_fixture_byte_count=fixture.byte_count,
            decision=decision,
        )

    def run_pilot_cell(
        self,
        invocation: _PilotInvocationV1,
        lease: CaseAuthorityBrokerV1,
    ) -> _PilotPortResultV1:
        self._require_pilot_scope_authorized()
        self._require_broker(lease)
        self._require_run_dispatch_allowed()
        unit_id = self._unit_id(invocation)
        with self._lock:
            state = self._units.get(unit_id)
        if state is None or state.environment is None or state.observation is None:
            raise ProductionDriverError("PILOT_RESET_MISSING", "cell reset did not complete")
        goal = state.task_goal
        if type(goal) is not str or not goal:
            raise ProductionDriverError("PILOT_TASK_GOAL_MISSING", "task goal is unavailable")
        self._begin_task_runtime(
            invocation=invocation,
            state=state,
            task_goal=goal,
            initial_observation=state.observation,
        )
        decisions: list[ActorDecisionEvidenceV1] = []
        pilot = getattr(self._manifest, "pilot", None)
        if type(pilot) is not FrozenPilotManifestV1:
            raise ProductionDriverError(
                "PILOT_SCOPE_UNAUTHORIZED",
                "smoke-only authority cannot enter pilot execution",
            )
        for actor_call_index in range(1, pilot.max_steps_per_cell + 1):
            self._require_deadline(invocation.deadline_monotonic_ns)
            self._require_run_dispatch_allowed()
            observation = state.observation
            agent = state.agent
            binding = state.task_binding
            if agent is None or type(binding) is not TaskAuditBinding:
                raise ProductionDriverError("ACTOR_RUNTIME_UNAVAILABLE", "pilot runtime is absent")
            context = self._step_context(
                state,
                step_index=actor_call_index,
                observation=observation,
            )
            with bind_audit_context(context):
                with self._bound_logical_call(
                    state,
                    context,
                    actor_call_index=actor_call_index,
                    expected_actor_request_sha256=None,
                ) as call:
                    self._require_resource_dispatch(
                        state.host,
                        ProductionDispatchKindV1.ACTOR,
                        deadline_ns=state.deadline_monotonic_ns,
                        state=state,
                    )
                    try:
                        with bind_sentinel_logical_call(call):
                            prediction, action = agent.predict(observation.model_dump())
                    except Exception:
                        self._journal_latest_audit_terminals(state)
                        raise
                    if type(prediction) is not str or type(action) is not JSONAction:
                        raise ProductionDriverError(
                            "INVALID_ACTOR_RESULT", "actor prediction/action type differs"
                        )
                    decision_ref = self._record_decision(state, context, prediction, action)
                    executable = action.action_type in _PILOT_GUI_ACTION_TYPES
                    if action.action_type not in _PILOT_GUI_ACTION_TYPES | {
                        FINISHED,
                        ENV_FAIL,
                        UNKNOWN,
                    }:
                        raise ProductionDriverError(
                            "PILOT_ACTION_FORBIDDEN",
                            "pilot permits only the closed GUI action vocabulary",
                        )
                    action_ns = 0
                    if executable:
                        execution = binding.capture.execution_started(decision=decision_ref)
                        started_ns = time.monotonic_ns()
                        try:
                            self._require_resource_dispatch(
                                state.host,
                                ProductionDispatchKindV1.ACTION,
                                deadline_ns=state.deadline_monotonic_ns,
                                state=state,
                            )
                            next_observation = state.environment.execute_action(action)
                        except Exception as exc:
                            action_ns = max(0, time.monotonic_ns() - started_ns)
                            binding.capture.transition_failed(
                                exception=exc,
                                execution=execution,
                                duration_ns=action_ns,
                            )
                            failed_action_receipt = self._receipt_for_action(
                                state,
                                action,
                                action_executed=False,
                                action_execution_ns=action_ns,
                            )
                            try:
                                self._decision_from_receipt(
                                    state,
                                    failed_action_receipt,
                                    actor_call_index=actor_call_index,
                                )
                            except ProductionDriverError:
                                # The raw terminal audit and attempt journal are already
                                # bound. Preserve the physical action failure as primary.
                                pass
                            raise
                        action_ns = max(0, time.monotonic_ns() - started_ns)
                        receipt = self._receipt_for_action(
                            state,
                            action,
                            action_executed=True,
                            action_execution_ns=action_ns,
                        )
                        decision_evidence = self._decision_from_receipt(
                            state,
                            receipt,
                            actor_call_index=actor_call_index,
                        )
                        if type(next_observation) is not Observation:
                            raise ProductionDriverError(
                                "INVALID_OBSERVATION", "action observation type differs"
                            )
                        binding.capture.transition_completed(
                            post_observation=next_observation.model_dump(),
                            execution=execution,
                            duration_ns=action_ns,
                            source_screenshot_bytes=_pil_png_bytes(next_observation.screenshot),
                        )
                        state.observation = next_observation
                    else:
                        receipt = self._receipt_for_action(
                            state,
                            action,
                            action_executed=False,
                            action_execution_ns=action_ns,
                        )
                        decision_evidence = self._decision_from_receipt(
                            state,
                            receipt,
                            actor_call_index=actor_call_index,
                        )
                        binding.capture.transition_not_executed(
                            reason="actor terminal or invalid action",
                            decision=decision_ref,
                        )
                    decisions.append(decision_evidence)
                    state.final_step_index = actor_call_index
                    if not executable:
                        break
        self._require_resource_dispatch(
            state.host,
            ProductionDispatchKindV1.SCORE,
            deadline_ns=state.deadline_monotonic_ns,
            state=state,
        )
        score, reason = state.environment.get_task_score(state.task_name)
        if (
            type(score) is not float
            or not math.isfinite(score)
            or not 0.0 <= score <= 1.0
            or type(reason) is not str
        ):
            raise ProductionDriverError("INVALID_OFFICIAL_RESULT", "official score differs")
        score_ppm = round(score * 1_000_000)
        reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()
        official = OfficialTaskResultEvidenceV1(
            task_id=invocation.cell.task_id,
            evaluator_id=OFFICIAL_RESULT_EVALUATOR_ID_V1,
            score_ppm=score_ppm,
            successful=score_ppm == 1_000_000,
            result_payload_sha256=_hash_projection(
                "production-official-result",
                cast(
                    JsonValue,
                    {
                        "evaluator_id": OFFICIAL_RESULT_EVALUATOR_ID_V1,
                        "reason_sha256": reason_hash,
                        "score_ppm": score_ppm,
                        "task_id": invocation.cell.task_id,
                    },
                ),
            ),
            reason_sha256=reason_hash,
        )
        state.score = score
        state.score_reason = reason
        state.completed = True
        return _PilotPortResultV1(decisions=tuple(decisions), official_result=official)

    def _unpublished_unit_evidence_for(self, unit_id: str) -> _UnpublishedUnitEvidenceV1 | None:
        archive = getattr(self, "_unpublished_unit_evidence", None)
        if archive is None:
            return None
        if type(archive) is not dict:
            raise ProductionDriverError(
                "UNIT_EVIDENCE_RECOVERY_INVALID",
                "unpublished unit evidence archive type differs",
            )
        with self._lock:
            recovery = archive.get(unit_id)
        if recovery is None:
            return None
        if (
            type(recovery) is not _UnpublishedUnitEvidenceV1
            or recovery.unit_id != unit_id
            or hashlib.sha256(recovery.raw).hexdigest() != recovery.raw_sha256
            or len(recovery.raw) != recovery.byte_count
            or type(recovery.durably_republished) is not bool
        ):
            raise ProductionDriverError(
                "UNIT_EVIDENCE_RECOVERY_INVALID",
                "unpublished unit evidence recovery binding differs",
            )
        return recovery

    def _release_unpublished_unit_evidence(
        self, invocation: _SmokeInvocationV1 | _PilotInvocationV1
    ) -> None:
        with self._lock:
            self._unpublished_unit_evidence.pop(self._unit_id(invocation), None)

    @staticmethod
    def _bind_evidence_publication_failure(
        evidence: dict[str, JsonValue],
        recovery: _UnpublishedUnitEvidenceV1,
    ) -> None:
        binding: dict[str, JsonValue] = {
            "full_unit_evidence_byte_count": recovery.byte_count,
            "full_unit_evidence_sha256": recovery.raw_sha256,
            "publication_failure_code": recovery.publication_failure_code,
            "status": (
                "PUBLICATION_FAILURE_DURABLY_RECOVERED"
                if recovery.durably_republished
                else "PUBLICATION_FAILED_IN_MEMORY_RECOVERED"
            ),
            "unit_id": recovery.unit_id,
        }
        binding["binding_sha256"] = _hash_projection(
            "production-unit-evidence-publication-failure", cast(JsonValue, binding)
        )
        evidence["evidence_publication_failure"] = cast(JsonValue, binding)
        evidence["evidence_publication_failure_code"] = recovery.publication_failure_code

    def failure_evidence_for_unit(
        self,
        invocation: _SmokeInvocationV1 | _PilotInvocationV1,
        *,
        failure_phase: str = "DISPATCH",
        failure_code: str = "UNIT_EXECUTION_FAILED",
    ) -> JsonValue | None:
        """Return the append-only terminal/decision journal for a failed unit."""

        unit_id = self._unit_id(invocation)
        with self._lock:
            state = self._units.get(unit_id)
            archived = self._unit_journals.get(unit_id)
        audit = None if state is None else state.runtime_audit
        publication_recovery: _UnpublishedUnitEvidenceV1 | None = None
        if state is not None:
            try:
                journal_raw = self._unit_journal_snapshot(state)
            except Exception:
                publication_recovery = self._unpublished_unit_evidence_for(unit_id)
                if publication_recovery is None:
                    raise
                evidence = self._decode_full_unit_journal_raw(
                    publication_recovery.raw, expected_unit_id=unit_id
                )
            else:
                evidence = self._decode_unit_journal_snapshot(journal_raw, expected_unit_id=unit_id)
                publication_recovery = self._unpublished_unit_evidence_for(unit_id)
        elif archived is not None:
            evidence = self._decode_unit_journal_snapshot(archived, expected_unit_id=unit_id)
            publication_recovery = self._unpublished_unit_evidence_for(unit_id)
        else:
            publication_recovery = self._unpublished_unit_evidence_for(unit_id)
            if publication_recovery is None:
                return None
            evidence = self._decode_full_unit_journal_raw(
                publication_recovery.raw, expected_unit_id=unit_id
            )
        evidence["failure_code"] = failure_code
        evidence["failure_phase"] = failure_phase
        if publication_recovery is not None:
            self._bind_evidence_publication_failure(evidence, publication_recovery)
        if audit is None:
            return cast(JsonValue, evidence)
        assert state is not None
        admission_failure = getattr(audit, "latest_admission_failure_receipt", None)
        if type(admission_failure) is ProductionRuntimeAuditAdmissionFailureReceiptV1:
            attempts, attempt_sha256s, attempt_failure = self._attempt_journal_for_call(
                state, admission_failure.logical_call_id
            )
            evidence["actor_admission_failure_receipt"] = cast(
                JsonValue,
                production_runtime_audit_admission_failure_receipt_projection(admission_failure),
            )
            evidence["actor_admission_failure_receipt_sha256"] = (
                production_runtime_audit_admission_failure_receipt_sha256(admission_failure)
            )
            evidence["attempt_journal_failure_code"] = attempt_failure
            evidence["live_attempt_receipts"] = cast(JsonValue, attempts)
            evidence["live_attempt_receipt_sha256s"] = cast(JsonValue, attempt_sha256s)
            return cast(JsonValue, evidence)
        commit_failure = audit.latest_commit_failure_receipt
        if type(commit_failure) is ProductionRuntimeAuditCommitFailureReceiptV1:
            attempts, attempt_sha256s, attempt_failure = self._attempt_journal_for_call(
                state, commit_failure.logical_call_id
            )
            evidence["actor_commit_failure_receipt"] = cast(
                JsonValue,
                production_runtime_audit_commit_failure_receipt_projection(commit_failure),
            )
            evidence["actor_commit_failure_receipt_sha256"] = (
                production_runtime_audit_commit_failure_receipt_sha256(commit_failure)
            )
            evidence["attempt_journal_failure_code"] = attempt_failure
            evidence["live_attempt_receipts"] = cast(JsonValue, attempts)
            evidence["live_attempt_receipt_sha256s"] = cast(JsonValue, attempt_sha256s)
            return cast(JsonValue, evidence)
        failure = audit.latest_failure_receipt
        if type(failure) is ProductionRuntimeAuditFailureReceiptV1:
            attempts, attempt_sha256s, attempt_failure = self._attempt_journal_for_call(
                state, failure.logical_call_id
            )
            evidence["actor_failure_receipt"] = cast(
                JsonValue, production_runtime_audit_failure_receipt_projection(failure)
            )
            evidence["actor_failure_receipt_sha256"] = (
                production_runtime_audit_failure_receipt_sha256(failure)
            )
            evidence["attempt_journal_failure_code"] = attempt_failure
            evidence["live_attempt_receipts"] = cast(JsonValue, attempts)
            evidence["live_attempt_receipt_sha256s"] = cast(JsonValue, attempt_sha256s)
            return cast(JsonValue, evidence)
        completed = audit.latest_completed_receipt
        if type(completed) is not ProductionRuntimeAuditReceiptV1:
            return cast(JsonValue, evidence)
        attempts, attempt_sha256s, attempt_failure = self._attempt_journal_for_call(
            state, completed.logical_call_id
        )
        evidence["actor_completed_receipt"] = cast(
            JsonValue, production_runtime_audit_receipt_projection(completed)
        )
        evidence["actor_completed_receipt_sha256"] = production_runtime_audit_receipt_sha256(
            completed
        )
        evidence["live_cost_exact"] = completed.live_cost_exact
        evidence["attempt_journal_failure_code"] = attempt_failure
        evidence["live_attempt_receipts"] = cast(JsonValue, attempts)
        evidence["live_attempt_receipt_sha256s"] = cast(JsonValue, attempt_sha256s)
        return cast(JsonValue, evidence)

    def _recoverable_failure_evidence_for_unit(
        self,
        invocation: _SmokeInvocationV1 | _PilotInvocationV1,
        *,
        failure_phase: str,
        failure_code: str,
    ) -> JsonValue:
        """Keep evidence-storage faults from pre-empting bounded cleanup."""

        try:
            return self.failure_evidence_for_unit(
                invocation,
                failure_phase=failure_phase,
                failure_code=failure_code,
            )
        except Exception as exc:
            projection: dict[str, JsonValue] = {
                "evidence_publication_failure_code": _exception_code(
                    exc, "UNIT_EVIDENCE_PUBLICATION_FAILED"
                ),
                "failure_code": failure_code,
                "failure_phase": failure_phase,
                "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                "status": "UNIT_EVIDENCE_RECOVERY_FAILED",
                "unit_id": self._unit_id(invocation),
            }
            projection["canonical_evidence_sha256"] = _hash_projection(
                "production-unit-evidence-recovery-failure", cast(JsonValue, projection)
            )
            return cast(JsonValue, projection)

    def cleanup_unit(self, invocation: _SmokeInvocationV1 | _PilotInvocationV1) -> _CleanupResultV1:
        unit_id = self._unit_id(invocation)
        with self._lock:
            state = self._units.get(unit_id)
        if state is None:
            raise ProductionDriverError("UNIT_RUNTIME_MISSING", "unit cleanup state is absent")
        if (
            state.deadline_monotonic_ns != invocation.deadline_monotonic_ns
            or state.cleanup_deadline_monotonic_ns != invocation.cleanup_deadline_monotonic_ns
            or state.authority_deadline_monotonic_ns != invocation.authority_deadline_monotonic_ns
            or state.attempt_termination_upper_bound_ns
            != invocation.attempt_termination_upper_bound_ns
        ):
            raise ProductionDriverError(
                "UNIT_DEADLINE_BINDING_MISMATCH",
                "unit cleanup invocation differs from its frozen deadlines",
            )
        journal_failure_code: str | None = None
        try:
            journal_raw = self._unit_journal_snapshot(state)
            with self._lock:
                prior_journal = self._unit_journals.setdefault(unit_id, journal_raw)
            if prior_journal != journal_raw:
                raise ProductionDriverError(
                    "UNIT_EVIDENCE_JOURNAL_DRIFT",
                    "per-unit terminal evidence changed across cleanup retry",
                )
        except Exception as exc:
            # Evidence publication is fail-closed for the unit result, but it
            # must never consume the bounded cleanup opportunity. Teardown,
            # process close, and Collector finalization continue below.
            journal_failure_code = _exception_code(exc, "UNIT_EVIDENCE_PUBLICATION_FAILED")
        cleanup_deadline_expired = time.monotonic_ns() >= state.cleanup_deadline_monotonic_ns
        cleanup_dispatch_failure_code: str | None = (
            "CLEANUP_DEADLINE_EXCEEDED" if cleanup_deadline_expired else None
        )
        failures: list[str] = ["CLEANUP_DEADLINE_EXCEEDED"] if cleanup_deadline_expired else []
        if journal_failure_code is not None:
            failures.append(journal_failure_code)
        teardown_result: object = None
        teardown_result_sha256: str | None = None
        teardown_attempted = False
        if state.environment is not None and not cleanup_deadline_expired:
            try:
                initialization_confirmed = state.environment.is_initialized
                if type(initialization_confirmed) is not bool:
                    raise ProductionDriverError(
                        "TASK_TEARDOWN_STATE_INVALID",
                        "environment initialization state is not an exact bool",
                    )
                if initialization_confirmed:
                    self._require_resource_dispatch(
                        state.host,
                        ProductionDispatchKindV1.CLEANUP,
                        deadline_ns=state.cleanup_deadline_monotonic_ns,
                        state=state,
                    )

                def mark_teardown_dispatched() -> None:
                    nonlocal teardown_attempted
                    teardown_attempted = True

                with state.environment.request_deadline_scope(state.cleanup_deadline_monotonic_ns):
                    teardown_result = state.environment.tear_down_task_if_initialized(
                        state.task_name,
                        dispatch_started=mark_teardown_dispatched,
                    )
                if type(teardown_result) is not CleanupTaskTeardownResultV1:
                    cleanup_dispatch_failure_code = "TASK_TEARDOWN_RESULT_INVALID"
                    failures.append(cleanup_dispatch_failure_code)
                elif (
                    type(cast(object, teardown_result.status)) is not CleanupTaskTeardownStatusV1
                    or type(cast(object, teardown_result.message)) is not str
                    or type(cast(object, teardown_result.request_dispatched)) is not bool
                    or teardown_result.request_dispatched != teardown_attempted
                ):
                    cleanup_dispatch_failure_code = "TASK_TEARDOWN_RESULT_INVALID"
                    failures.append(cleanup_dispatch_failure_code)
                elif teardown_result.status is CleanupTaskTeardownStatusV1.NOT_INITIALIZED_NO_IO:
                    state.cleanup_recovery_outcome = {
                        "initialization_permitted": False,
                        "message_sha256": hashlib.sha256(
                            teardown_result.message.encode("utf-8")
                        ).hexdigest(),
                        "outcome": teardown_result.status.value,
                        "request_dispatched": teardown_result.request_dispatched,
                        "teardown_attempted": teardown_attempted,
                    }
                    cleanup_dispatch_failure_code = "TASK_TEARDOWN_NOT_INITIALIZED_NO_IO"
                    failures.append(cleanup_dispatch_failure_code)
                elif teardown_result.status is not CleanupTaskTeardownStatusV1.SUCCEEDED:
                    state.cleanup_recovery_outcome = {
                        "initialization_permitted": False,
                        "message_sha256": hashlib.sha256(
                            teardown_result.message.encode("utf-8")
                        ).hexdigest(),
                        "outcome": teardown_result.status.value,
                        "request_dispatched": teardown_result.request_dispatched,
                        "teardown_attempted": teardown_attempted,
                    }
                    if time.monotonic_ns() >= state.cleanup_deadline_monotonic_ns:
                        cleanup_dispatch_failure_code = "CLEANUP_DEADLINE_EXCEEDED"
                        failures.append(cleanup_dispatch_failure_code)
                    else:
                        failures.append("TASK_TEARDOWN_REJECTED")
                else:
                    state.cleanup_recovery_outcome = {
                        "initialization_permitted": False,
                        "message_sha256": hashlib.sha256(
                            teardown_result.message.encode("utf-8")
                        ).hexdigest(),
                        "outcome": teardown_result.status.value,
                        "request_dispatched": teardown_result.request_dispatched,
                        "teardown_attempted": teardown_attempted,
                    }
                    if not teardown_attempted or not teardown_result.request_dispatched:
                        cleanup_dispatch_failure_code = "TASK_TEARDOWN_RESULT_INVALID"
                        failures.append(cleanup_dispatch_failure_code)
                    else:
                        teardown_result_sha256 = _hash_projection(
                            "production-task-teardown-result",
                            cast(
                                JsonValue,
                                {
                                    "message_sha256": hashlib.sha256(
                                        teardown_result.message.encode("utf-8")
                                    ).hexdigest(),
                                    "request_dispatched": teardown_result.request_dispatched,
                                    "status": teardown_result.status.value,
                                    "task_name": state.task_name,
                                },
                            ),
                        )
            except Exception as exc:
                cleanup_dispatch_failure_code = (
                    "CLEANUP_DEADLINE_EXCEEDED"
                    if time.monotonic_ns() >= state.cleanup_deadline_monotonic_ns
                    else _exception_code(exc, "TASK_TEARDOWN_FAILED")
                )
                failures.append(cleanup_dispatch_failure_code)
            try:
                state.environment.close()
            except Exception:
                failures.append("ENVIRONMENT_CLOSE_FAILED")
        elif state.environment is not None:
            try:
                state.environment.close()
            except Exception:
                failures.append("ENVIRONMENT_CLOSE_FAILED")
        if state.agent is not None:
            try:
                state.agent.done()
                close = getattr(state.agent.openai_client, "close", None)
                if callable(close):
                    close()
            except Exception:
                failures.append("AGENT_CLOSE_FAILED")
        final_manifest_hash: str | None = None
        task_run_id: str | None = None
        if state.lifecycle is not None and state.task_binding is not None:
            binding = state.task_binding
            task_run_id = binding.metadata.task_run_id
            runtime_status = "completed" if state.completed and not failures else "crashed"
            try:
                binding.capture.end_task(
                    runtime_status=runtime_status,
                    termination_source="production_driver",
                    final_step_index=state.final_step_index,
                    score=state.score,
                    reason=state.score_reason,
                    teardown_attempted=teardown_attempted,
                    teardown_result=teardown_result,
                    token_usage=(
                        {} if state.agent is None else state.agent.get_total_token_usage()
                    ),
                )
                if not binding.capture.capture_complete:
                    failures.append("COLLECTOR_INCOMPLETE")
            except Exception:
                failures.append("COLLECTOR_END_TASK_FAILED")
            try:
                state.lifecycle.finish_task_attempt(
                    binding=binding,
                    result=None,
                    exception=None,
                    retry_planned=False,
                    runtime_status=runtime_status,
                )
            except Exception:
                failures.append("COLLECTOR_FINISH_ATTEMPT_FAILED")
            try:
                final_path = state.lifecycle.finalize(runtime_status=runtime_status)
                if final_path is None or not final_path.is_file() or final_path.is_symlink():
                    failures.append("COLLECTOR_FINALIZE_FAILED")
                else:
                    raw = final_path.read_bytes()
                    final_manifest_hash = hashlib.sha256(raw).hexdigest()
            except Exception:
                failures.append("COLLECTOR_FINALIZE_FAILED")
        if (
            cleanup_dispatch_failure_code is None
            and time.monotonic_ns() >= state.cleanup_deadline_monotonic_ns
        ):
            cleanup_dispatch_failure_code = "CLEANUP_DEADLINE_EXCEEDED"
            failures.append(cleanup_dispatch_failure_code)
        final_unit_journal_preimage: bytes | None = None
        final_unit_journal_validation: _ValidatedUnitJournalReferenceV1 | None = None
        resource_lifecycle = getattr(self, "_resource_lifecycle", None)
        if (
            not failures
            and type(resource_lifecycle) is ProductionResourceLifecycleAdapterV1
            and resource_lifecycle.resource_topology
            is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        ):
            try:
                final_unit_journal_preimage = self._unit_journal_snapshot(state)
                final_unit_journal_validation = self._validated_smoke_unit_journal_reference(
                    final_unit_journal_preimage, expected_unit_id=unit_id
                )
                with self._lock:
                    self._unit_journals[unit_id] = final_unit_journal_preimage
            except Exception as exc:
                journal_failure_code = _exception_code(exc, "UNIT_EVIDENCE_PUBLICATION_FAILED")
                failures.append(journal_failure_code)
        if failures:
            if cleanup_dispatch_failure_code == "CLEANUP_DEADLINE_EXCEEDED":
                with self._lock:
                    self._units.pop(unit_id, None)
            raise ProductionDriverError(
                cleanup_dispatch_failure_code or journal_failure_code or "UNIT_CLEANUP_FAILED",
                "production unit cleanup failed: " + ",".join(failures),
            )
        with self._lock:
            self._units.pop(unit_id, None)
        final_unit_journal_sha256 = (
            None
            if final_unit_journal_preimage is None
            else hashlib.sha256(final_unit_journal_preimage).hexdigest()
        )
        return _CleanupResultV1(
            cleanup_receipt_sha256=_hash_projection(
                "production-unit-cleanup",
                cast(
                    JsonValue,
                    {
                        "cleanup_dispatch_authorized": (
                            teardown_attempted if state.environment is not None else None
                        ),
                        "collector_manifest_sha256": final_manifest_hash,
                        "deadline_binding": cast(
                            JsonValue,
                            _deadline_projection(
                                execution_deadline=state.deadline_monotonic_ns,
                                cleanup_deadline=state.cleanup_deadline_monotonic_ns,
                                authority_deadline=state.authority_deadline_monotonic_ns,
                                attempt_termination_upper_bound_ns=(
                                    state.attempt_termination_upper_bound_ns
                                ),
                            ),
                        ),
                        "manifest_sha256": invocation.manifest_sha256,
                        "task_run_id": task_run_id,
                        "teardown_attempted": teardown_attempted,
                        "teardown_result_sha256": teardown_result_sha256,
                        "unit_journal_sha256": final_unit_journal_sha256,
                        "unit_id": unit_id,
                    },
                ),
            ),
            unit_journal_preimage=final_unit_journal_preimage,
            unit_journal_sha256=final_unit_journal_sha256,
            unit_journal_validated_reference=final_unit_journal_validation,
        )


type _FixedExecutionPortV1 = _CpuFixedExecutionPortV1 | _ProductionFixedExecutionPortV1


def _require_module_port(value: object) -> _FixedExecutionPortV1:
    if type(value) not in {_CpuFixedExecutionPortV1, _ProductionFixedExecutionPortV1}:
        raise ValueError("execution port is not the module-owned exact implementation")
    return cast(_FixedExecutionPortV1, value)


def _validate_smoke_decision(case: LiveSmokeCaseV1, value: ActorDecisionEvidenceV1) -> None:
    if type(value) is not ActorDecisionEvidenceV1:
        raise ProductionDriverError("INVALID_PORT_RESULT", "smoke decision type differs")
    if value.census.actor_calls != 1 or value.census.openai_calls > case.max_openai_calls:
        raise ProductionDriverError("SMOKE_CENSUS_MISMATCH", "smoke call census differs")
    if value.census.actor_actions != 0 or value.executed_action_sha256 is not None:
        raise ProductionDriverError("SMOKE_ACTION_FORBIDDEN", "smoke executed an action")
    if case.mode in {SmokeModeV1.OFF, SmokeModeV1.SHADOW} and (
        value.final_request_sha256 != value.raw_request_sha256
    ):
        raise ProductionDriverError(
            "SMOKE_ORIGINAL_PARITY_FAILED", "OFF/SHADOW final request differs from Original"
        )
    if case.mode is SmokeModeV1.OFF and (
        value.census.openai_calls != 0 or value.census.offline_rubric_evaluations != 0
    ):
        raise ProductionDriverError("OFF_SEMANTIC_WORK_FORBIDDEN", "OFF performed semantic work")
    if case.mode is not SmokeModeV1.OFF:
        no_history_first_call = value.history_policy_attempt_receipt_sha256 is None
        expected_history_calls = 0 if no_history_first_call else 1
        if (
            value.census.offline_rubric_evaluations != 0
            or value.census.rubric_openai_calls != 2
            or value.census.history_policy_openai_calls != expected_history_calls
            or value.census.openai_calls != 2 + expected_history_calls
            or len(value.rubric_attempt_receipt_sha256s) != 2
        ):
            raise ProductionDriverError(
                "SMOKE_CENSUS_MISMATCH",
                "semantic smoke did not bind rubric-first or typed no-history census",
            )
    if value.census.cost_usd_micros > case.max_cost_usd_micros or (
        value.census.wall_time_ms > case.max_wall_time_seconds * 1000
    ):
        raise ProductionDriverError("SMOKE_BUDGET_EXCEEDED", "smoke case exceeded its budget")


def _validate_pilot_decisions(
    pilot: FrozenPilotManifestV1,
    cell: PilotCellV1,
    values: tuple[ActorDecisionEvidenceV1, ...],
) -> DriverStageCensusV1:
    if type(values) is not tuple or not values or len(values) > pilot.max_steps_per_cell:
        raise ProductionDriverError("INVALID_PORT_RESULT", "pilot decision count differs")
    if any(type(value) is not ActorDecisionEvidenceV1 for value in values):
        raise ProductionDriverError("INVALID_PORT_RESULT", "pilot decision type differs")
    if tuple(value.actor_call_index for value in values) != tuple(range(1, len(values) + 1)):
        raise ProductionDriverError("PILOT_CALL_ORDER_MISMATCH", "pilot call order differs")
    for value in values:
        if cell.arm is PilotArmV1.BASELINE and (
            value.final_request_sha256 != value.raw_request_sha256
            or value.census.openai_calls != 0
            or value.census.offline_rubric_evaluations != 0
        ):
            raise ProductionDriverError(
                "BASELINE_ISOLATION_FAILED", "baseline performed Sentinel semantic work"
            )
        expected_rubric_calls = 2 if value.actor_call_index == 1 else 1
        if cell.arm is PilotArmV1.JOINT_SENTINEL:
            no_history_first_call = (
                value.actor_call_index == 1 and value.history_policy_attempt_receipt_sha256 is None
            )
            expected_history_calls = 0 if no_history_first_call else 1
            if (
                value.census.offline_rubric_evaluations != 0
                or value.census.rubric_openai_calls != expected_rubric_calls
                or value.census.history_policy_openai_calls != expected_history_calls
                or value.census.openai_calls != expected_rubric_calls + expected_history_calls
                or len(value.rubric_attempt_receipt_sha256s) != expected_rubric_calls
                or (
                    value.actor_call_index > 1
                    and value.history_policy_attempt_receipt_sha256 is None
                )
            ):
                raise ProductionDriverError(
                    "PILOT_OPENAI_ROLE_CENSUS_MISMATCH",
                    "joint actor call did not bind rubric and typed history-policy census",
                )
    census = _sum_census(tuple(value.census for value in values))
    if census.wall_time_ms > pilot.per_cell_timeout_seconds * 1000:
        raise ProductionDriverError("PILOT_CELL_TIMEOUT", "pilot cell exceeded its timeout")
    return census


class FixedLiveSmokeAdapterV1:
    """Sealed adapter implementing ``LiveSmokeAdapterPortV1``."""

    __slots__ = ("_evidence", "_failure_evidence", "_lock", "_port")

    def __init__(self, port: object, *, seal: object) -> None:
        if seal is not _MODULE_SEAL:
            raise ValueError("live-smoke adapter is module-owned")
        self._port = _require_module_port(port)
        self._evidence: dict[RunStageV1, SmokeStageEvidenceV1] = {}
        self._failure_evidence: dict[RunStageV1, bytes] = {}
        self._lock = threading.Lock()

    def run_host(
        self,
        host: PilotHostV1,
        plan: HostLiveSmokePlanV1,
        actor_resource: SnapshotResourceV1,
        openai_stages: tuple[OpenAIResponsesStageV1, ...],
        context: StageAdapterContextV1,
        lease: CaseAuthorityBrokerV1,
    ) -> AdapterStageResultV1:
        with self._lock:
            if type(host) is not PilotHostV1:
                raise ProductionDriverError("UNTRUSTED_TYPE", "host must use exact enum")
            trusted_context = _snapshot_context(context)
            trusted_plan = _snapshot_plan(plan)
            trusted_resource = _snapshot_resource(actor_resource)
            _, policy_sha = _history_policy_stage(openai_stages)
            _validate_lease(lease, trusted_context.manifest_sha256)
            if trusted_plan.host is not host or trusted_resource.host is not host:
                raise ProductionDriverError("SMOKE_HOST_MISMATCH", "smoke host binding differs")
            stage = _stage_for_host(host)
            if stage in self._evidence:
                raise ProductionDriverError("STAGE_ALREADY_RUN", "smoke host already completed")
            self._failure_evidence[stage] = canonical_json_bytes(
                cast(
                    JsonValue,
                    {
                        "completed_case_ids": [],
                        "completed_records": [],
                        "completed_records_sha256": _hash_projection(
                            "smoke-completed-unit-journal", cast(JsonValue, [])
                        ),
                        "failure_code": "SMOKE_CASE_EXECUTION_FAILED",
                        "manifest_sha256": trusted_context.manifest_sha256,
                        "run_id": trusted_context.run_id,
                        "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                        "stage": stage.value,
                        "status": "FAILED",
                    },
                )
            )
            _validate_smoke_reservation(trusted_plan, trusted_context)
            resource_sha = _resource_sha256(trusted_resource)
            records: list[SmokeCaseEvidenceV1] = []
            for index, case in enumerate(trusted_plan.cases):
                unit_started_ns = time.monotonic_ns()
                execution_deadline_ns, cleanup_deadline_ns = _freeze_unit_deadlines(
                    unit_started_ns=unit_started_ns,
                    unit_timeout_seconds=case.max_wall_time_seconds,
                    authority_deadline_monotonic_ns=(
                        trusted_context.authority_deadline_monotonic_ns
                    ),
                    shutdown_grace_seconds=self._port.shutdown_grace_seconds,
                    attempt_termination_upper_bound_ns=(
                        self._port.attempt_termination_upper_bound_ns
                    ),
                )
                invocation = _SmokeInvocationV1(
                    manifest_sha256=trusted_context.manifest_sha256,
                    run_id=trusted_context.run_id,
                    source_commit=trusted_context.source_commit,
                    host=host,
                    sequence_index=index,
                    case=case,
                    actor_resource_sha256=resource_sha,
                    history_policy_stage_sha256=policy_sha,
                    deadline_monotonic_ns=execution_deadline_ns,
                    cleanup_deadline_monotonic_ns=cleanup_deadline_ns,
                    authority_deadline_monotonic_ns=(
                        trusted_context.authority_deadline_monotonic_ns
                    ),
                    attempt_termination_upper_bound_ns=(
                        self._port.attempt_termination_upper_bound_ns
                    ),
                )
                port_result: _SmokePortResultV1 | None = None
                dispatch_error: Exception | None = None
                unit_failure_evidence: JsonValue | None = None
                try:
                    candidate = self._port.run_smoke_case(invocation, lease)
                    if type(candidate) is not _SmokePortResultV1:
                        raise ProductionDriverError(
                            "INVALID_PORT_RESULT", "smoke port result type differs"
                        )
                    port_result = candidate
                except Exception as exc:
                    dispatch_error = exc
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        unit_failure_evidence = self._port._recoverable_failure_evidence_for_unit(
                            invocation,
                            failure_phase="DISPATCH",
                            failure_code=_exception_code(exc, "SMOKE_CASE_EXECUTION_FAILED"),
                        )
                cleanup: _CleanupResultV1 | None = None
                cleanup_error: Exception | None = None
                cleanup_attempted = True
                try:
                    candidate_cleanup = self._port.cleanup_unit(invocation)
                    if type(candidate_cleanup) is not _CleanupResultV1:
                        raise ProductionDriverError(
                            "INVALID_PORT_RESULT", "cleanup result type differs"
                        )
                    cleanup = candidate_cleanup
                except Exception as exc:
                    cleanup_error = exc
                if cleanup_error is not None:
                    cleanup_failure_code = _exception_code(cleanup_error, "UNIT_CLEANUP_FAILED")
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        unit_failure_evidence = self._port._recoverable_failure_evidence_for_unit(
                            invocation,
                            failure_phase="CLEANUP",
                            failure_code=cleanup_failure_code,
                        )
                    self._failure_evidence[stage] = _smoke_unit_failure_preimage(
                        context=trusted_context,
                        stage=stage,
                        records=records,
                        invocation=invocation,
                        port_result=port_result,
                        cleanup=cleanup,
                        cleanup_attempted=cleanup_attempted,
                        cleanup_error=cleanup_error,
                        dispatch_error=dispatch_error,
                        failure_phase="CLEANUP",
                        failure_code=cleanup_failure_code,
                        unit_failure_evidence=unit_failure_evidence,
                    )
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        self._port._release_unpublished_unit_evidence(invocation)
                    raise ProductionDriverError(
                        cleanup_failure_code, "smoke unit cleanup failed closed"
                    ) from None
                if dispatch_error is not None or port_result is None:
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        unit_failure_evidence = self._port._recoverable_failure_evidence_for_unit(
                            invocation,
                            failure_phase="DISPATCH",
                            failure_code="SMOKE_CASE_EXECUTION_FAILED",
                        )
                    self._failure_evidence[stage] = _smoke_unit_failure_preimage(
                        context=trusted_context,
                        stage=stage,
                        records=records,
                        invocation=invocation,
                        port_result=port_result,
                        cleanup=cleanup,
                        cleanup_attempted=cleanup_attempted,
                        cleanup_error=None,
                        dispatch_error=dispatch_error,
                        failure_phase="DISPATCH",
                        failure_code="SMOKE_CASE_EXECUTION_FAILED",
                        unit_failure_evidence=unit_failure_evidence,
                    )
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        self._port._release_unpublished_unit_evidence(invocation)
                    raise ProductionDriverError(
                        "SMOKE_CASE_EXECUTION_FAILED", "smoke case failed closed"
                    ) from None
                assert cleanup is not None
                try:
                    if (
                        port_result.request_fixture_sha256 != case.request_fixture_sha256
                        or port_result.request_fixture_byte_count != case.request_fixture_byte_count
                    ):
                        raise ProductionDriverError(
                            "SMOKE_FIXTURE_MISMATCH", "loaded fixture differs from authority"
                        )
                    _validate_smoke_decision(case, port_result.decision)
                    measured_wall_ms = max(
                        port_result.decision.census.wall_time_ms,
                        (time.monotonic_ns() - unit_started_ns + 999_999) // 1_000_000,
                    )
                    if measured_wall_ms > case.max_wall_time_seconds * 1_000:
                        raise ProductionDriverError(
                            "SMOKE_BUDGET_EXCEEDED", "smoke case wall budget elapsed"
                        )
                    case_census = replace(
                        _sum_census((port_result.decision.census,)),
                        wall_time_ms=measured_wall_ms,
                    )
                    record = SmokeCaseEvidenceV1(
                        manifest_sha256=trusted_context.manifest_sha256,
                        run_id=trusted_context.run_id,
                        stage=stage,
                        host=host,
                        sequence_index=index,
                        case_id=case.case_id,
                        task_id=case.task_id,
                        mode=case.mode,
                        actor_resource_sha256=resource_sha,
                        history_policy_stage_sha256=policy_sha,
                        request_fixture_sha256=port_result.request_fixture_sha256,
                        request_fixture_byte_count=port_result.request_fixture_byte_count,
                        decision=port_result.decision,
                        cleanup_receipt_sha256=cleanup.cleanup_receipt_sha256,
                        unit_journal_preimage=cleanup.unit_journal_preimage,
                        unit_journal_sha256=cleanup.unit_journal_sha256,
                        unit_journal_validated_reference=(cleanup.unit_journal_validated_reference),
                        census=case_census,
                    )
                except Exception as exc:
                    failure_code = _exception_code(exc, "SMOKE_POST_DISPATCH_ADMISSION_FAILED")
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        unit_failure_evidence = self._port._recoverable_failure_evidence_for_unit(
                            invocation,
                            failure_phase="POST_DISPATCH_ADMISSION",
                            failure_code=failure_code,
                        )
                    self._failure_evidence[stage] = _smoke_unit_failure_preimage(
                        context=trusted_context,
                        stage=stage,
                        records=records,
                        invocation=invocation,
                        port_result=port_result,
                        cleanup=cleanup,
                        cleanup_attempted=cleanup_attempted,
                        cleanup_error=None,
                        dispatch_error=None,
                        failure_phase="POST_DISPATCH_ADMISSION",
                        failure_code=failure_code,
                        unit_failure_evidence=unit_failure_evidence,
                    )
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        self._port._release_unpublished_unit_evidence(invocation)
                    if isinstance(exc, ProductionDriverError):
                        raise
                    raise ProductionDriverError(
                        "SMOKE_POST_DISPATCH_ADMISSION_FAILED",
                        "smoke evidence admission failed closed",
                    ) from exc
                records.append(record)
                completed_records = [_smoke_case_evidence_projection(item) for item in records]
                self._failure_evidence[stage] = canonical_json_bytes(
                    cast(
                        JsonValue,
                        {
                            "completed_case_ids": [item.case_id for item in records],
                            "completed_records": completed_records,
                            "completed_records_sha256": _hash_projection(
                                "smoke-completed-unit-journal",
                                cast(JsonValue, completed_records),
                            ),
                            "failure_code": "SMOKE_STAGE_INTERRUPTED",
                            "manifest_sha256": trusted_context.manifest_sha256,
                            "run_id": trusted_context.run_id,
                            "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                            "stage": stage.value,
                            "status": "IN_PROGRESS",
                        },
                    )
                )
            census = _sum_census(tuple(item.census for item in records))
            evidence = SmokeStageEvidenceV1(
                manifest_sha256=trusted_context.manifest_sha256,
                run_id=trusted_context.run_id,
                stage=stage,
                host=host,
                actor_resource_sha256=resource_sha,
                history_policy_stage_sha256=policy_sha,
                cases=tuple(records),
                census=census,
                schema_version=(
                    PRODUCTION_SHARED_SMOKE_EVIDENCE_SCHEMA_VERSION_V2
                    if any(item.unit_journal_preimage is not None for item in records)
                    else PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION
                ),
            )
            self._evidence[stage] = evidence
            evidence_preimage = canonical_json_bytes(
                cast(JsonValue, smoke_stage_evidence_projection(evidence))
            )
            return AdapterStageResultV1(
                stage=stage,
                manifest_sha256=trusted_context.manifest_sha256,
                evidence_sha256=smoke_stage_evidence_sha256(evidence),
                evidence_preimage=evidence_preimage,
                actor_calls=census.actor_calls,
                openai_calls=census.openai_calls,
                actor_actions=census.actor_actions,
                cost_usd_micros=census.cost_usd_micros,
                completed_units=tuple(
                    f"{host.value}:{case.mode.value}" for case in trusted_plan.cases
                ),
                provider_final_request_proven=True,
            )

    def evidence_for_stage(self, stage: RunStageV1) -> SmokeStageEvidenceV1 | None:
        with self._lock:
            return self._evidence.get(stage)

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None:
        with self._lock:
            return self._failure_evidence.get(stage)


class FixedPilotAdapterV1:
    """Sealed adapter implementing ``PilotAdapterPortV1``."""

    __slots__ = ("_evidence", "_failure_evidence", "_lock", "_port")

    def __init__(self, port: object, *, seal: object) -> None:
        if seal is not _MODULE_SEAL:
            raise ValueError("pilot adapter is module-owned")
        self._port = _require_module_port(port)
        self._evidence: PilotStageEvidenceV1 | None = None
        self._failure_evidence: bytes | None = None
        self._lock = threading.Lock()

    def run_pilot(
        self,
        pilot: FrozenPilotManifestV1,
        actor_resources: tuple[SnapshotResourceV1, ...],
        openai_stages: tuple[OpenAIResponsesStageV1, ...],
        context: StageAdapterContextV1,
        lease: CaseAuthorityBrokerV1,
    ) -> AdapterStageResultV1:
        with self._lock:
            if self._evidence is not None:
                raise ProductionDriverError("STAGE_ALREADY_RUN", "pilot already completed")
            trusted_context = _snapshot_context(context)
            if trusted_context.sequence_execution_scope != "R24_R25_FULL":
                raise ProductionDriverError(
                    "PILOT_SCOPE_UNAUTHORIZED",
                    "smoke-only authority cannot enter pilot execution",
                )
            trusted_pilot = _snapshot_pilot(pilot)
            self._failure_evidence = canonical_json_bytes(
                cast(
                    JsonValue,
                    {
                        "completed_cell_indices": [],
                        "completed_records": [],
                        "completed_records_sha256": _hash_projection(
                            "pilot-completed-unit-journal", cast(JsonValue, [])
                        ),
                        "failure_code": "PILOT_CELL_EXECUTION_FAILED",
                        "manifest_sha256": trusted_context.manifest_sha256,
                        "run_id": trusted_context.run_id,
                        "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                        "stage": RunStageV1.R25_PILOT.value,
                        "status": "FAILED",
                    },
                )
            )
            if type(actor_resources) is not tuple:
                raise ProductionDriverError("UNTRUSTED_TYPE", "resources must use exact tuple")
            resources = tuple(_snapshot_resource(value) for value in actor_resources)
            if tuple(value.host for value in resources) != (
                PilotHostV1.QWEN3_VL,
                PilotHostV1.MAI_UI,
            ):
                raise ProductionDriverError(
                    "PILOT_RESOURCE_MATRIX_MISMATCH", "pilot resources must be Qwen then MAI"
                )
            _, policy_sha = _history_policy_stage(openai_stages)
            _validate_lease(lease, trusted_context.manifest_sha256)
            _validate_pilot_reservation(trusted_pilot, trusted_context)
            self._port.prepare_pilot(trusted_pilot)
            resource_hashes = {value.host: _resource_sha256(value) for value in resources}
            resources_sha = _hash_projection(
                "actor-resource-matrix",
                cast(
                    JsonValue,
                    [
                        {
                            "host": value.host.value,
                            "resource_sha256": resource_hashes[value.host],
                        }
                        for value in resources
                    ],
                ),
            )
            records: list[PilotCellEvidenceV1] = []
            effective_reset_states: dict[str, str] = {}
            for index, cell in enumerate(trusted_pilot.cells):
                unit_started_ns = time.monotonic_ns()
                execution_deadline_ns, cleanup_deadline_ns = _freeze_unit_deadlines(
                    unit_started_ns=unit_started_ns,
                    unit_timeout_seconds=trusted_pilot.per_cell_timeout_seconds,
                    authority_deadline_monotonic_ns=(
                        trusted_context.authority_deadline_monotonic_ns
                    ),
                    shutdown_grace_seconds=self._port.shutdown_grace_seconds,
                    attempt_termination_upper_bound_ns=(
                        self._port.attempt_termination_upper_bound_ns
                    ),
                )
                invocation = _PilotInvocationV1(
                    manifest_sha256=trusted_context.manifest_sha256,
                    run_id=trusted_context.run_id,
                    source_commit=trusted_context.source_commit,
                    sequence_index=index,
                    cell=cell,
                    actor_resource_sha256=resource_hashes[cell.host],
                    history_policy_stage_sha256=policy_sha,
                    deadline_monotonic_ns=execution_deadline_ns,
                    cleanup_deadline_monotonic_ns=cleanup_deadline_ns,
                    authority_deadline_monotonic_ns=(
                        trusted_context.authority_deadline_monotonic_ns
                    ),
                    attempt_termination_upper_bound_ns=(
                        self._port.attempt_termination_upper_bound_ns
                    ),
                )
                reset: _PilotResetResultV1 | None = None
                port_result: _PilotPortResultV1 | None = None
                dispatch_error: Exception | None = None
                unit_failure_evidence: JsonValue | None = None
                try:
                    candidate_reset = self._port.reset_pilot_cell(invocation)
                    if type(candidate_reset) is not _PilotResetResultV1:
                        raise ProductionDriverError(
                            "INVALID_PORT_RESULT", "pilot reset result type differs"
                        )
                    reset = candidate_reset
                    candidate_result = self._port.run_pilot_cell(invocation, lease)
                    if type(candidate_result) is not _PilotPortResultV1:
                        raise ProductionDriverError(
                            "INVALID_PORT_RESULT", "pilot port result type differs"
                        )
                    port_result = candidate_result
                except Exception as exc:
                    dispatch_error = exc
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        unit_failure_evidence = self._port._recoverable_failure_evidence_for_unit(
                            invocation,
                            failure_phase="DISPATCH",
                            failure_code=_exception_code(exc, "PILOT_CELL_EXECUTION_FAILED"),
                        )
                cleanup: _CleanupResultV1 | None = None
                cleanup_error: Exception | None = None
                cleanup_attempted = True
                try:
                    candidate_cleanup = self._port.cleanup_unit(invocation)
                    if type(candidate_cleanup) is not _CleanupResultV1:
                        raise ProductionDriverError(
                            "INVALID_PORT_RESULT", "cleanup result type differs"
                        )
                    cleanup = candidate_cleanup
                except Exception as exc:
                    cleanup_error = exc
                if cleanup_error is not None:
                    cleanup_failure_code = _exception_code(cleanup_error, "UNIT_CLEANUP_FAILED")
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        unit_failure_evidence = self._port._recoverable_failure_evidence_for_unit(
                            invocation,
                            failure_phase="CLEANUP",
                            failure_code=cleanup_failure_code,
                        )
                    self._failure_evidence = _pilot_unit_failure_preimage(
                        context=trusted_context,
                        records=records,
                        invocation=invocation,
                        reset=reset,
                        port_result=port_result,
                        cleanup=cleanup,
                        cleanup_attempted=cleanup_attempted,
                        cleanup_error=cleanup_error,
                        dispatch_error=dispatch_error,
                        failure_phase="CLEANUP",
                        failure_code=cleanup_failure_code,
                        unit_failure_evidence=unit_failure_evidence,
                    )
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        self._port._release_unpublished_unit_evidence(invocation)
                    raise ProductionDriverError(
                        cleanup_failure_code, "pilot unit cleanup failed closed"
                    ) from None
                if dispatch_error is not None or reset is None or port_result is None:
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        unit_failure_evidence = self._port._recoverable_failure_evidence_for_unit(
                            invocation,
                            failure_phase="DISPATCH",
                            failure_code="PILOT_CELL_EXECUTION_FAILED",
                        )
                    self._failure_evidence = _pilot_unit_failure_preimage(
                        context=trusted_context,
                        records=records,
                        invocation=invocation,
                        reset=reset,
                        port_result=port_result,
                        cleanup=cleanup,
                        cleanup_attempted=cleanup_attempted,
                        cleanup_error=None,
                        dispatch_error=dispatch_error,
                        failure_phase="DISPATCH",
                        failure_code="PILOT_CELL_EXECUTION_FAILED",
                        unit_failure_evidence=unit_failure_evidence,
                    )
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        self._port._release_unpublished_unit_evidence(invocation)
                    raise ProductionDriverError(
                        "PILOT_CELL_EXECUTION_FAILED", "pilot cell failed closed"
                    ) from None
                assert cleanup is not None
                try:
                    _require_sha256(reset.reset_receipt_sha256, "reset_receipt_sha256")
                    _require_sha256(
                        reset.effective_reset_state_sha256,
                        "effective_reset_state_sha256",
                    )
                    prior_reset_state = effective_reset_states.setdefault(
                        cell.task_id, reset.effective_reset_state_sha256
                    )
                    if prior_reset_state != reset.effective_reset_state_sha256:
                        raise ProductionDriverError(
                            "PILOT_EFFECTIVE_RESET_MISMATCH",
                            "matched cells did not start from one exact effective state",
                        )
                    _require_sha256(cleanup.cleanup_receipt_sha256, "cleanup_receipt_sha256")
                    cell_census = _validate_pilot_decisions(
                        trusted_pilot, cell, port_result.decisions
                    )
                    measured_wall_ms = max(
                        cell_census.wall_time_ms,
                        (time.monotonic_ns() - unit_started_ns + 999_999) // 1_000_000,
                    )
                    if measured_wall_ms > trusted_pilot.per_cell_timeout_seconds * 1_000:
                        raise ProductionDriverError(
                            "PILOT_CELL_TIMEOUT", "pilot cell wall budget elapsed"
                        )
                    cell_census = replace(cell_census, wall_time_ms=measured_wall_ms)
                    if (
                        type(port_result.official_result) is not OfficialTaskResultEvidenceV1
                        or port_result.official_result.task_id != cell.task_id
                    ):
                        raise ProductionDriverError(
                            "INVALID_OFFICIAL_RESULT",
                            "official result is absent or task-mismatched",
                        )
                    record = PilotCellEvidenceV1(
                        manifest_sha256=trusted_context.manifest_sha256,
                        run_id=trusted_context.run_id,
                        sequence_index=index,
                        task_id=cell.task_id,
                        task_parameters_sha256=cell.task_parameters_sha256,
                        reset_seed=cell.reset_seed,
                        host=cell.host,
                        arm=cell.arm,
                        sentinel_mode=cell.sentinel_mode,
                        actor_resource_sha256=resource_hashes[cell.host],
                        history_policy_stage_sha256=policy_sha,
                        reset_receipt_sha256=reset.reset_receipt_sha256,
                        effective_reset_state_sha256=reset.effective_reset_state_sha256,
                        decisions=port_result.decisions,
                        official_result=port_result.official_result,
                        cleanup_receipt_sha256=cleanup.cleanup_receipt_sha256,
                        census=cell_census,
                    )
                    cumulative = _sum_census(tuple(item.census for item in (*records, record)))
                    if (
                        cumulative.actor_calls > trusted_pilot.max_total_actor_calls
                        or cumulative.openai_calls > trusted_pilot.max_total_openai_calls
                        or cumulative.cost_usd_micros > trusted_pilot.max_total_cost_usd_micros
                        or cumulative.wall_time_ms
                        > trusted_pilot.max_total_wall_time_seconds * 1000
                    ):
                        raise ProductionDriverError(
                            "PILOT_BUDGET_EXCEEDED",
                            "pilot cumulative census exceeded authority",
                        )
                except Exception as exc:
                    failure_code = _exception_code(exc, "PILOT_POST_DISPATCH_ADMISSION_FAILED")
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        unit_failure_evidence = self._port._recoverable_failure_evidence_for_unit(
                            invocation,
                            failure_phase="POST_DISPATCH_ADMISSION",
                            failure_code=failure_code,
                        )
                    self._failure_evidence = _pilot_unit_failure_preimage(
                        context=trusted_context,
                        records=records,
                        invocation=invocation,
                        reset=reset,
                        port_result=port_result,
                        cleanup=cleanup,
                        cleanup_attempted=cleanup_attempted,
                        cleanup_error=None,
                        dispatch_error=None,
                        failure_phase="POST_DISPATCH_ADMISSION",
                        failure_code=failure_code,
                        unit_failure_evidence=unit_failure_evidence,
                    )
                    if type(self._port) is _ProductionFixedExecutionPortV1:
                        self._port._release_unpublished_unit_evidence(invocation)
                    if isinstance(exc, ProductionDriverError):
                        raise
                    raise ProductionDriverError(
                        "PILOT_POST_DISPATCH_ADMISSION_FAILED",
                        "pilot evidence admission failed closed",
                    ) from exc
                records.append(record)
                completed_records = [_pilot_cell_evidence_projection(item) for item in records]
                self._failure_evidence = canonical_json_bytes(
                    cast(
                        JsonValue,
                        {
                            "completed_cell_indices": [item.sequence_index for item in records],
                            "completed_records": completed_records,
                            "completed_records_sha256": _hash_projection(
                                "pilot-completed-unit-journal",
                                cast(JsonValue, completed_records),
                            ),
                            "failure_code": "PILOT_STAGE_INTERRUPTED",
                            "manifest_sha256": trusted_context.manifest_sha256,
                            "run_id": trusted_context.run_id,
                            "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                            "stage": RunStageV1.R25_PILOT.value,
                            "status": "IN_PROGRESS",
                        },
                    )
                )
            census = _sum_census(tuple(record.census for record in records))
            evidence = PilotStageEvidenceV1(
                manifest_sha256=trusted_context.manifest_sha256,
                run_id=trusted_context.run_id,
                pilot_manifest_sha256=frozen_pilot_manifest_sha256(trusted_pilot),
                actor_resources_sha256=resources_sha,
                history_policy_stage_sha256=policy_sha,
                cells=tuple(records),
                census=census,
            )
            self._evidence = evidence
            evidence_preimage = canonical_json_bytes(
                cast(JsonValue, pilot_stage_evidence_projection(evidence))
            )
            return AdapterStageResultV1(
                stage=RunStageV1.R25_PILOT,
                manifest_sha256=trusted_context.manifest_sha256,
                evidence_sha256=pilot_stage_evidence_sha256(evidence),
                evidence_preimage=evidence_preimage,
                actor_calls=census.actor_calls,
                openai_calls=census.openai_calls,
                actor_actions=census.actor_actions,
                cost_usd_micros=census.cost_usd_micros,
                completed_units=tuple(
                    f"pilot-cell-{index:03d}" for index in range(len(trusted_pilot.cells))
                ),
                provider_final_request_proven=True,
            )

    @property
    def evidence(self) -> PilotStageEvidenceV1 | None:
        with self._lock:
            return self._evidence

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None:
        if stage is not RunStageV1.R25_PILOT:
            return None
        with self._lock:
            return self._failure_evidence


class ProductionDriverAdaptersV1:
    """Identity-sealed smoke/pilot pair over one exact module-owned port."""

    __slots__ = ("_pilot", "_port", "_smoke")

    def __init__(
        self,
        *,
        port: object,
        seal: object,
    ) -> None:
        if seal is not _MODULE_SEAL:
            raise ValueError("production driver bundle is module-owned")
        trusted_port = _require_module_port(port)
        self._port = trusted_port
        self._smoke = FixedLiveSmokeAdapterV1(trusted_port, seal=_MODULE_SEAL)
        self._pilot = FixedPilotAdapterV1(trusted_port, seal=_MODULE_SEAL)

    @property
    def smoke(self) -> FixedLiveSmokeAdapterV1:
        return self._smoke

    @property
    def pilot(self) -> FixedPilotAdapterV1:
        return self._pilot

    @property
    def resource_lifecycle(self) -> ProductionResourceLifecycleAdapterV1 | None:
        if type(self._port) is not _ProductionFixedExecutionPortV1:
            return None
        return self._port._resource_lifecycle

    @property
    def cpu_trace(self) -> CpuProductionDriverTraceV1:
        if type(self._port) is not _CpuFixedExecutionPortV1:
            raise ProductionDriverError(
                "CPU_TRACE_UNAVAILABLE", "production execution port has no CPU trace"
            )
        return self._port.trace


def build_cpu_test_production_driver_v1(
    fault: CpuProductionDriverFaultV1 = CpuProductionDriverFaultV1.NONE,
) -> ProductionDriverAdaptersV1:
    """Build deterministic adapters without I/O or executable dependency injection."""

    if type(fault) is not CpuProductionDriverFaultV1:
        raise ValueError("fault must use the exact closed CPU enum")
    port = _CpuFixedExecutionPortV1(fault, seal=_MODULE_SEAL)
    return ProductionDriverAdaptersV1(
        port=port,
        seal=_MODULE_SEAL,
    )


def build_production_driver_v1(
    *,
    factory: ProductionPostPreflightFactoryV1,
    runtime_config: ProductionRuntimeConfigV1,
    confirmed_runtime_config_sha256: str,
    pricing: LiveAttemptPricingV1,
    confirmed_pricing_sha256: str,
    production_audit_sink: ExternalProductionRuntimeAuditSinkV1,
    resource_lifecycle: ProductionResourceLifecycleAdapterV1,
) -> ProductionDriverAdaptersV1:
    """Construct the exact production host port without starting any resource."""

    if type(factory) is not ProductionPostPreflightFactoryV1:
        raise ProductionDriverError(
            "POST_PREFLIGHT_FACTORY_REQUIRED", "exact post-preflight factory is required"
        )
    if type(runtime_config) is not ProductionRuntimeConfigV1 or (
        confirmed_runtime_config_sha256 != production_runtime_config_sha256(runtime_config)
    ):
        raise ProductionDriverError(
            "RUNTIME_CONFIG_HASH_DRIFT", "confirmed runtime config hash differs"
        )
    if type(resource_lifecycle) is not ProductionResourceLifecycleAdapterV1:
        raise ProductionDriverError(
            "RESOURCE_LIFECYCLE_REQUIRED",
            "exact shared production resource lifecycle is required",
        )
    shared_binding_claimed = (
        factory.sequence_execution_scope.value == "R24_LIVE_SMOKE_ONLY"
        or runtime_config.resource_topology
        is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        or resource_lifecycle.resource_topology
        is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
    )
    if shared_binding_claimed and (
        factory.sequence_execution_scope.value != "R24_LIVE_SMOKE_ONLY"
        or factory.runtime_config_sha256 != confirmed_runtime_config_sha256
        or resource_lifecycle.runtime_config_sha256 != confirmed_runtime_config_sha256
        or runtime_config.resource_topology
        is not ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        or resource_lifecycle.resource_topology
        is not ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
    ):
        raise ProductionDriverError(
            "SHARED_RESOURCE_AUTHORITY_MISMATCH",
            "shared resource scope/runtime/lifecycle authority differs",
        )
    if type(pricing) is not LiveAttemptPricingV1 or (
        confirmed_pricing_sha256 != live_attempt_pricing_sha256(pricing)
        or confirmed_pricing_sha256 != factory.pricing_binding_sha256
    ):
        raise ProductionDriverError(
            "PRICING_BINDING_MISMATCH", "confirmed pricing authority differs"
        )
    if type(production_audit_sink) is not ExternalProductionRuntimeAuditSinkV1:
        raise ProductionDriverError(
            "PRODUCTION_AUDIT_SINK_REQUIRED", "exact external production audit sink is required"
        )
    port = _ProductionFixedExecutionPortV1(
        factory=factory,
        config=runtime_config,
        pricing=pricing,
        audit_sink=production_audit_sink,
        budget_ledger=build_production_live_budget_ledger_v1(factory),
        resource_lifecycle=resource_lifecycle,
        seal=_PRODUCTION_INSTALLATION_SEAL,
    )
    return ProductionDriverAdaptersV1(port=port, seal=_MODULE_SEAL)


def production_driver_available_v1() -> bool:
    """The concrete builder exists; execution still needs owner-pinned dependencies."""

    return True


def _smoke_protocol_assertion(value: FixedLiveSmokeAdapterV1) -> LiveSmokeAdapterPortV1:
    return value


def _pilot_protocol_assertion(value: FixedPilotAdapterV1) -> PilotAdapterPortV1:
    return value


__all__ = [
    "OFFICIAL_RESULT_EVALUATOR_ID_V1",
    "PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION",
    "PRODUCTION_MODEL_HANDOFF_EVIDENCE_SCHEMA_VERSION_V1",
    "PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1",
    "PRODUCTION_RESOURCE_CLEANUP_EVIDENCE_SCHEMA_VERSION_V1",
    "PRODUCTION_SHARED_RESOURCE_EVIDENCE_SCHEMA_VERSION_V2",
    "PRODUCTION_SHARED_SMOKE_EVIDENCE_SCHEMA_VERSION_V2",
    "PRODUCTION_DRIVER_REQUIRED_BINDINGS_V1",
    "PRODUCTION_DRIVER_REQUIRED_HOOKS_V1",
    "ActorDecisionEvidenceV1",
    "CpuProductionDriverFaultV1",
    "CpuResourceLifecycleFaultV1",
    "CpuProductionDriverTraceV1",
    "DriverCallCensusV1",
    "DriverStageCensusV1",
    "FixedLiveSmokeAdapterV1",
    "FixedPilotAdapterV1",
    "OfficialTaskResultEvidenceV1",
    "PilotCellEvidenceV1",
    "PilotStageEvidenceV1",
    "CpuResourceLifecycleTraceV1",
    "OwnedProcessIdentityV1",
    "ProductionCommandSpecV1",
    "ProductionDriverAdaptersV1",
    "ProductionDriverError",
    "ProductionDriverHookV1",
    "ProductionDispatchKindV1",
    "ProductionModelHandoffEvidenceV1",
    "ProductionModelStopEvidenceV1",
    "ProductionResourceLifecycleAdapterV1",
    "ProductionResourceStageEvidenceV1",
    "ProductionResourceTopologyV1",
    "ProductionRuntimeConfigV1",
    "ProductionSharedGpuAttestationV1",
    "SharedGpuProcessEvidenceV1",
    "SmokeCaseEvidenceV1",
    "SmokeStageEvidenceV1",
    "build_cpu_test_production_driver_v1",
    "build_cpu_test_resource_lifecycle_adapter_v1",
    "build_production_driver_v1",
    "build_production_case_authority_broker_provider_v1",
    "build_production_resource_lifecycle_adapter_v1",
    "pilot_stage_evidence_projection",
    "pilot_stage_evidence_sha256",
    "production_command_spec_projection",
    "production_command_spec_sha256",
    "production_driver_available_v1",
    "production_model_handoff_evidence_projection",
    "production_model_handoff_evidence_sha256",
    "production_model_stop_evidence_projection",
    "production_model_stop_evidence_sha256",
    "production_resource_stage_evidence_projection",
    "production_resource_stage_evidence_sha256",
    "parse_production_runtime_config",
    "production_runtime_config_projection",
    "production_runtime_config_sha256",
    "production_shared_gpu_attestation_projection",
    "production_shared_gpu_attestation_sha256",
    "smoke_stage_evidence_projection",
    "smoke_stage_evidence_sha256",
]
