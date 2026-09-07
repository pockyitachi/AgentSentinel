"""Fail-closed execution boundary for the authorized R2.4/R2.5 sequence.

The public production executor accepts only an identity-sealed bundle assembled
from exact module-owned production adapter types.  A data-only CPU implementation
exercises ordering, accounting, secret-lease lifetime, external-output
transactions, and cleanup without reading a secret or invoking a provider,
GPU, model service, Docker, MobileWorld backend, or GUI action.

Production mechanisms remain exact sealed ports rather than command strings;
callers cannot turn an authority manifest into an arbitrary shell-command
surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, cast, runtime_checkable

from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V2,
    R24_R25_SEQUENCE_RESULT_SCHEMA_VERSION,
    HostLiveSmokePlanV1,
    LiveRunContractError,
    OpenAIResponsesStageV1,
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SequenceRunResultV1,
    SequenceStageExecutorV1,
    SequenceStatusV1,
    SmokeModeV1,
    SnapshotResourceV1,
    StageExecutionReceiptV1,
    authority_manifest_projection,
    authority_manifest_sha256,
    parse_authority_manifest,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS,
    R24_SMOKE_SEQUENCE_RESULT_SCHEMA_VERSION,
    R24SmokeRunAuthorityManifestV1,
    SequenceExecutionScopeV1,
    parse_smoke_authority_manifest,
    smoke_authority_manifest_projection,
    smoke_authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import FrozenPilotManifestV1, PilotHostV1

LIVE_EXECUTOR_BINDING_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-r2.5-executor-binding/v1"
R24_SMOKE_EXECUTOR_BINDING_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-smoke-executor-binding/v1"
)
R24_SMOKE_MAI_TERMINAL_EVIDENCE_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-smoke-mai-terminal-evidence/v1"
)
R24_SMOKE_MAI_FAILURE_EVIDENCE_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-smoke-mai-failure-evidence/v1"
)


class ExecutorStateV1(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class CpuTestFaultV1(StrEnum):
    """Closed, non-executable fault vocabulary for CPU contract tests."""

    NONE = "NONE"
    QWEN_SMOKE_FAILURE = "QWEN_SMOKE_FAILURE"
    PILOT_ACTOR_BUDGET_OVERRUN = "PILOT_ACTOR_BUDGET_OVERRUN"
    SECRET_LEASE_UNAVAILABLE = "SECRET_LEASE_UNAVAILABLE"
    RESOURCE_CLEANUP_FAILURE = "RESOURCE_CLEANUP_FAILURE"
    QWEN_SMOKE_AND_RESOURCE_CLEANUP_FAILURE = "QWEN_SMOKE_AND_RESOURCE_CLEANUP_FAILURE"
    QWEN_TO_MAI_HANDOFF_FAILURE = "QWEN_TO_MAI_HANDOFF_FAILURE"
    MAI_SMOKE_FAILURE = "MAI_SMOKE_FAILURE"
    QWEN_CASE_BROKER_CLOSE_FAILURE = "QWEN_CASE_BROKER_CLOSE_FAILURE"
    MAI_CASE_BROKER_CLOSE_FAILURE = "MAI_CASE_BROKER_CLOSE_FAILURE"
    PILOT_CASE_BROKER_CLOSE_FAILURE = "PILOT_CASE_BROKER_CLOSE_FAILURE"


@dataclass(frozen=True, slots=True)
class AdapterStageResultV1:
    """Secret-free census returned by one trusted stage adapter."""

    stage: RunStageV1
    manifest_sha256: str
    evidence_sha256: str
    evidence_preimage: bytes
    actor_calls: int
    openai_calls: int
    actor_actions: int
    cost_usd_micros: int
    completed_units: tuple[str, ...]
    provider_final_request_proven: bool

    def __post_init__(self) -> None:
        if type(self.stage) is not RunStageV1:
            raise ValueError("stage must use the exact RunStageV1 enum")
        for name in ("manifest_sha256", "evidence_sha256"):
            value = getattr(self, name)
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if type(self.evidence_preimage) is not bytes or not self.evidence_preimage:
            raise ValueError("evidence_preimage must be nonempty canonical JSON bytes")
        if len(self.evidence_preimage) > 64 * 1024 * 1024:
            raise ValueError("evidence_preimage exceeds its hard byte bound")
        try:
            evidence = json.loads(self.evidence_preimage.decode("utf-8"))
        except Exception as exc:
            raise ValueError("evidence_preimage is not UTF-8 JSON") from exc
        if type(evidence) is not dict or _canonical_bytes(evidence) != self.evidence_preimage:
            raise ValueError("evidence_preimage is not an exact canonical JSON object")
        if hashlib.sha256(self.evidence_preimage).hexdigest() != self.evidence_sha256:
            raise ValueError("evidence_preimage hash differs from evidence_sha256")
        for name in ("actor_calls", "openai_calls", "actor_actions", "cost_usd_micros"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if type(self.completed_units) is not tuple or any(
            type(unit) is not str for unit in self.completed_units
        ):
            raise ValueError("completed_units must be an exact tuple of strings")
        if type(self.provider_final_request_proven) is not bool:
            raise ValueError("provider proof flag must be bool")


@dataclass(frozen=True, slots=True)
class StageAdapterContextV1:
    """Minimal immutable authority passed to a module-owned adapter."""

    manifest_sha256: str
    sequence_execution_scope: str
    sequence_scope_authority_sha256: str
    run_id: str
    source_commit: str
    remaining_actor_calls: int
    remaining_openai_calls: int
    remaining_cost_usd_micros: int
    remaining_wall_time_ms: int
    authority_deadline_monotonic_ns: int

    def __post_init__(self) -> None:
        if (
            not _is_lower_hex(self.manifest_sha256, 64)
            or not _is_lower_hex(self.sequence_scope_authority_sha256, 64)
            or not _is_lower_hex(self.source_commit, 40)
        ):
            raise ValueError("stage context hashes are invalid")
        if self.sequence_execution_scope not in {
            "R24_R25_FULL",
            "R24_LIVE_SMOKE_ONLY",
        }:
            raise ValueError("stage context execution scope is invalid")
        if type(self.run_id) is not str or not self.run_id:
            raise ValueError("stage context run_id is invalid")
        for name in (
            "remaining_actor_calls",
            "remaining_openai_calls",
            "remaining_cost_usd_micros",
            "remaining_wall_time_ms",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if (
            type(self.authority_deadline_monotonic_ns) is not int
            or self.authority_deadline_monotonic_ns <= 0
        ):
            raise ValueError("authority deadline must be a positive monotonic timestamp")


@dataclass(frozen=True, slots=True)
class ExecutorCensusV1:
    state: ExecutorStateV1
    manifest_sha256: str
    completed_stages: tuple[RunStageV1, ...]
    actor_calls: int
    openai_calls: int
    actor_actions: int
    cost_usd_micros: int
    wall_time_ms: int
    secret_leases_acquired: int
    secret_leases_closed: int
    cleanup_attempted: bool
    cleanup_succeeded: bool
    output_committed: bool


@dataclass(frozen=True, slots=True)
class R24SmokeSequenceResultV1:
    """Closed terminal result for the three-stage R2.4-only smoke sequence."""

    schema_version: str
    execution_scope: SequenceExecutionScopeV1
    run_id: str
    source_commit: str
    manifest_sha256: str
    runtime_config_sha256: str
    preflight_report_sha256: str
    factory_binding_sha256: str
    status: SequenceStatusV1
    completed_stages: tuple[RunStageV1, ...]
    receipts: tuple[StageExecutionReceiptV1, ...]
    pilot_executed: bool
    resource_cleanup_status: str
    resource_cleanup_evidence_sha256: str
    resource_cleanup_upper_bound_seconds: int
    resource_cleanup_upper_bound_preimage: bytes
    resource_cleanup_upper_bound_sha256: str
    total_wall_time_ms: int
    terminal_output_published: bool
    successful_output_committed: bool
    failed_stage: RunStageV1 | None
    failure_code: str | None

    def __post_init__(self) -> None:
        if self.schema_version != R24_SMOKE_SEQUENCE_RESULT_SCHEMA_VERSION:
            raise ValueError("unknown R2.4 smoke sequence-result schema")
        if self.execution_scope is not SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY:
            raise ValueError("R2.4 smoke result scope differs")
        if type(self.run_id) is not str or not self.run_id:
            raise ValueError("R2.4 smoke result run_id is invalid")
        if not _is_lower_hex(self.source_commit, 40):
            raise ValueError("R2.4 smoke result source commit is invalid")
        for name in (
            "manifest_sha256",
            "runtime_config_sha256",
            "preflight_report_sha256",
            "factory_binding_sha256",
            "resource_cleanup_evidence_sha256",
            "resource_cleanup_upper_bound_sha256",
        ):
            if not _is_lower_hex(getattr(self, name), 64):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if type(self.status) is not SequenceStatusV1:
            raise ValueError("R2.4 smoke result status is untrusted")
        if type(self.completed_stages) is not tuple or type(self.receipts) is not tuple:
            raise ValueError("R2.4 smoke result stage collections are untrusted")
        if any(type(stage) is not RunStageV1 for stage in self.completed_stages) or any(
            type(receipt) is not StageExecutionReceiptV1 for receipt in self.receipts
        ):
            raise ValueError("R2.4 smoke result contains an untrusted stage")
        expected_stages = (
            RunStageV1.RESOURCE_PREFLIGHT,
            RunStageV1.QWEN_LIVE_SMOKE,
            RunStageV1.MAI_LIVE_SMOKE,
        )
        if (
            self.completed_stages != tuple(receipt.stage for receipt in self.receipts)
            or self.completed_stages != expected_stages[: len(self.completed_stages)]
            or any(receipt.manifest_sha256 != self.manifest_sha256 for receipt in self.receipts)
        ):
            raise ValueError("R2.4 smoke result stage binding differs")
        if self.pilot_executed is not False:
            raise ValueError("R2.4 smoke result cannot claim pilot execution")
        if self.resource_cleanup_status not in {"SUCCEEDED", "RETRY_REQUIRED"}:
            raise ValueError("R2.4 smoke cleanup status is invalid")
        if (
            type(self.resource_cleanup_upper_bound_seconds) is not int
            or self.resource_cleanup_upper_bound_seconds < R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS
            or type(self.resource_cleanup_upper_bound_preimage) is not bytes
            or not self.resource_cleanup_upper_bound_preimage
            or hashlib.sha256(self.resource_cleanup_upper_bound_preimage).hexdigest()
            != self.resource_cleanup_upper_bound_sha256
        ):
            raise ValueError("R2.4 smoke cleanup upper bound is invalid")
        try:
            cleanup_upper_bound = json.loads(
                self.resource_cleanup_upper_bound_preimage.decode("utf-8")
            )
        except Exception as exc:
            raise ValueError("R2.4 smoke cleanup upper bound is not JSON") from exc
        if (
            type(cleanup_upper_bound) is not dict
            or _canonical_bytes(cleanup_upper_bound) != self.resource_cleanup_upper_bound_preimage
            or type(cleanup_upper_bound.get("value")) is not dict
            or cleanup_upper_bound["value"].get("cleanup_upper_bound_seconds")
            != self.resource_cleanup_upper_bound_seconds
        ):
            raise ValueError("R2.4 smoke cleanup upper bound preimage differs")
        if type(self.total_wall_time_ms) is not int or self.total_wall_time_ms < 0:
            raise ValueError("R2.4 smoke total wall time is invalid")
        if self.terminal_output_published is not True:
            raise ValueError("R2.4 smoke terminal result must be durably published")
        if type(self.successful_output_committed) is not bool:
            raise ValueError("R2.4 smoke output commit flag is invalid")
        if self.status is SequenceStatusV1.COMPLETE:
            if (
                self.completed_stages != expected_stages
                or not all(receipt.passed for receipt in self.receipts)
                or self.resource_cleanup_status != "SUCCEEDED"
                or not self.successful_output_committed
                or self.failed_stage is not None
                or self.failure_code is not None
            ):
                raise ValueError("complete R2.4 smoke result is not terminally bound")
        elif (
            self.successful_output_committed
            or type(self.failed_stage) is not RunStageV1
            or self.failed_stage not in expected_stages
            or type(self.failure_code) is not str
            or not self.failure_code
        ):
            raise ValueError("failed R2.4 smoke result needs a typed terminal failure")


def r24_smoke_sequence_result_projection(
    value: R24SmokeSequenceResultV1,
) -> dict[str, object]:
    if type(value) is not R24SmokeSequenceResultV1:
        raise TypeError("R2.4 smoke sequence result must use the exact type")
    trusted = value
    return {
        "completed_stages": [stage.value for stage in trusted.completed_stages],
        "execution_scope": trusted.execution_scope.value,
        "factory_binding_sha256": trusted.factory_binding_sha256,
        "failed_stage": None if trusted.failed_stage is None else trusted.failed_stage.value,
        "failure_code": trusted.failure_code,
        "manifest_sha256": trusted.manifest_sha256,
        "pilot_executed": trusted.pilot_executed,
        "preflight_report_sha256": trusted.preflight_report_sha256,
        "receipts": [_receipt_projection(receipt) for receipt in trusted.receipts],
        "resource_cleanup_evidence_sha256": trusted.resource_cleanup_evidence_sha256,
        "resource_cleanup_status": trusted.resource_cleanup_status,
        "resource_cleanup_upper_bound_seconds": (trusted.resource_cleanup_upper_bound_seconds),
        "resource_cleanup_upper_bound": json.loads(trusted.resource_cleanup_upper_bound_preimage),
        "resource_cleanup_upper_bound_sha256": (trusted.resource_cleanup_upper_bound_sha256),
        "run_id": trusted.run_id,
        "runtime_config_sha256": trusted.runtime_config_sha256,
        "schema_version": trusted.schema_version,
        "source_commit": trusted.source_commit,
        "status": trusted.status.value,
        "successful_output_committed": trusted.successful_output_committed,
        "terminal_output_published": trusted.terminal_output_published,
        "total_wall_time_ms": trusted.total_wall_time_ms,
    }


def r24_smoke_sequence_result_sha256(value: R24SmokeSequenceResultV1) -> str:
    return hashlib.sha256(_canonical_bytes(r24_smoke_sequence_result_projection(value))).hexdigest()


@dataclass(frozen=True, slots=True)
class CaseExecutionLeaseBindingV1:
    """Secret-free projection of one post-preflight, request-bound lease."""

    manifest_sha256: str
    preflight_report_sha256: str
    factory_binding_sha256: str
    pricing_binding_sha256: str
    case_execution_lease_sha256: str
    execution_scope: str
    openai_stage_set_sha256: str
    stage: RunStageV1
    host: PilotHostV1
    mode: SmokeModeV1
    case_id: str
    task_id: str
    task_parameters_sha256: str | None
    reset_seed: int | None
    actor_call_index: int
    request_sha256: str
    issued_at_utc: str
    expires_at_utc: str

    def __post_init__(self) -> None:
        for name in (
            "manifest_sha256",
            "preflight_report_sha256",
            "factory_binding_sha256",
            "pricing_binding_sha256",
            "case_execution_lease_sha256",
            "openai_stage_set_sha256",
            "request_sha256",
        ):
            if not _is_lower_hex(getattr(self, name), 64):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if type(self.stage) is not RunStageV1 or type(self.host) is not PilotHostV1:
            raise ValueError("case lease stage/host must use exact enums")
        if type(self.mode) is not SmokeModeV1:
            raise ValueError("case lease mode must use the exact enum")
        if self.execution_scope not in {"OWNER_AUTHORIZED_LIVE", "CPU_TEST_LOCAL"}:
            raise ValueError("case lease execution scope is invalid")
        if type(self.case_id) is not str or not self.case_id:
            raise ValueError("case lease ID is invalid")
        if type(self.task_id) is not str or not self.task_id:
            raise ValueError("case lease task ID is invalid")
        if self.task_parameters_sha256 is not None and not _is_lower_hex(
            self.task_parameters_sha256, 64
        ):
            raise ValueError("case lease task parameters hash is invalid")
        if self.reset_seed is not None and (
            type(self.reset_seed) is not int or self.reset_seed < 0
        ):
            raise ValueError("case lease reset seed is invalid")
        if type(self.actor_call_index) is not int or self.actor_call_index < 1:
            raise ValueError("case lease actor call index is invalid")
        if type(self.issued_at_utc) is not str or type(self.expires_at_utc) is not str:
            raise ValueError("case lease timestamps are invalid")


@runtime_checkable
class SecureSecretLeaseV1(Protocol):
    """Opaque per-attempt lease: deliberately exposes no secret accessor."""

    @property
    def manifest_sha256(self) -> str: ...

    @property
    def environment_key(self) -> str: ...

    def close(self) -> None: ...


@runtime_checkable
class CaseAuthorityBrokerV1(Protocol):
    """Stage-held broker that mints authority only after a raw request exists."""

    @property
    def manifest_sha256(self) -> str: ...

    @property
    def environment_key(self) -> str: ...

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
    ) -> CaseExecutionLeaseBindingV1: ...

    def close(self) -> None: ...


@runtime_checkable
class CaseAuthorityBrokerProviderPortV1(Protocol):
    """Root hook for a post-preflight case-authority broker."""

    def acquire(
        self,
        reference: SecretFileReferenceV1,
        *,
        manifest_sha256: str,
    ) -> CaseAuthorityBrokerV1: ...


@runtime_checkable
class SecureSecretLeaseProviderPortV1(CaseAuthorityBrokerProviderPortV1, Protocol):
    """Compatibility protocol; acquired objects are now no-secret brokers."""


@runtime_checkable
class ResourceLifecycleAdapterPortV1(Protocol):
    """Root hook for verified GPU/model/Docker/backend lifecycle ownership."""

    def prepare(
        self,
        resources: tuple[SnapshotResourceV1, ...],
        context: StageAdapterContextV1,
    ) -> AdapterStageResultV1: ...

    def cleanup(self, context: StageAdapterContextV1) -> None: ...

    def handoff_to_mai(self, context: StageAdapterContextV1) -> AdapterStageResultV1: ...

    def cleanup_success_evidence_preimage(self) -> bytes | None: ...

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None: ...


@runtime_checkable
class LiveSmokeAdapterPortV1(Protocol):
    """Root hook for one host's fixed OFF/SHADOW/ACTIVE live smoke."""

    def run_host(
        self,
        host: PilotHostV1,
        plan: HostLiveSmokePlanV1,
        actor_resource: SnapshotResourceV1,
        openai_stages: tuple[OpenAIResponsesStageV1, ...],
        context: StageAdapterContextV1,
        lease: CaseAuthorityBrokerV1,
    ) -> AdapterStageResultV1: ...

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None: ...


@runtime_checkable
class PilotAdapterPortV1(Protocol):
    """Root hook for the frozen matched MobileWorld pilot matrix."""

    def run_pilot(
        self,
        pilot: FrozenPilotManifestV1,
        actor_resources: tuple[SnapshotResourceV1, ...],
        openai_stages: tuple[OpenAIResponsesStageV1, ...],
        context: StageAdapterContextV1,
        lease: CaseAuthorityBrokerV1,
    ) -> AdapterStageResultV1: ...

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None: ...


class UnavailableSecureSecretLeaseProviderV1:
    """Default broker provider: fail closed without inspecting the path."""

    def acquire(
        self,
        reference: SecretFileReferenceV1,
        *,
        manifest_sha256: str,
    ) -> CaseAuthorityBrokerV1:
        del reference, manifest_sha256
        raise RuntimeError("case authority broker provider is unavailable")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_lower_hex(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _receipt_projection(receipt: StageExecutionReceiptV1) -> dict[str, object]:
    return {
        "actor_actions": receipt.actor_actions,
        "actor_calls": receipt.actor_calls,
        "completed_units": list(receipt.completed_units),
        "cost_usd_micros": receipt.cost_usd_micros,
        "evidence_sha256": receipt.evidence_sha256,
        "manifest_sha256": receipt.manifest_sha256,
        "openai_calls": receipt.openai_calls,
        "passed": receipt.passed,
        "provider_final_request_proven": receipt.provider_final_request_proven,
        "stage": receipt.stage.value,
        "wall_time_ms": receipt.wall_time_ms,
    }


def _sequence_result_projection(value: SequenceRunResultV1) -> dict[str, object]:
    if type(value) is not SequenceRunResultV1:
        raise TypeError("full sequence result must use the exact trusted type")
    return {
        "failed_stage": None if value.failed_stage is None else value.failed_stage.value,
        "failure_code": value.failure_code,
        "manifest_sha256": value.manifest_sha256,
        "receipts": [_receipt_projection(receipt) for receipt in value.receipts],
        "run_id": value.run_id,
        "schema_version": value.schema_version,
        "status": value.status.value,
    }


class AtomicExternalOutputTransactionV1:
    """One all-or-nothing, repository-external sequence output transaction."""

    _STAGE_FILES: Final[dict[RunStageV1, str]] = {
        RunStageV1.RESOURCE_PREFLIGHT: "00-resource-preflight.json",
        RunStageV1.QWEN_LIVE_SMOKE: "01-qwen-live-smoke.json",
        RunStageV1.MAI_LIVE_SMOKE: "02-mai-live-smoke.json",
        RunStageV1.R25_PILOT: "03-r25-pilot.json",
    }

    @staticmethod
    def _open_directory_chain(
        path: Path,
    ) -> tuple[Path, tuple[tuple[int, str | None, int, int], ...]]:
        """Open and retain every path component so rename/symlink swaps are visible."""

        resolved = path.resolve(strict=True)
        if not resolved.is_absolute():
            raise ValueError("output parent must be absolute")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        opened: list[tuple[int, str | None, int, int]] = []
        try:
            descriptor = os.open("/", flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("filesystem root is not a directory")
            opened.append((descriptor, None, metadata.st_dev, metadata.st_ino))
            for component in resolved.parts[1:]:
                descriptor = os.open(component, flags, dir_fd=opened[-1][0])
                metadata = os.fstat(descriptor)
                named = os.stat(component, dir_fd=opened[-1][0], follow_symlinks=False)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or not stat.S_ISDIR(named.st_mode)
                    or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
                ):
                    os.close(descriptor)
                    raise ValueError("output parent component identity differs")
                opened.append((descriptor, component, metadata.st_dev, metadata.st_ino))
            return resolved, tuple(opened)
        except Exception:
            for descriptor, _, _, _ in reversed(opened):
                os.close(descriptor)
            raise

    @staticmethod
    def _close_directory_chain(
        chain: tuple[tuple[int, str | None, int, int], ...],
    ) -> None:
        for descriptor, _, _, _ in reversed(chain):
            try:
                os.close(descriptor)
            except OSError:
                pass

    @staticmethod
    def _entry_exists(directory_descriptor: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def __init__(
        self,
        *,
        output_root: Path,
        repository_root: Path,
        run_id: str,
        source_commit: str,
        manifest_sha256: str,
        pricing_sha256: str | None = None,
        sentinel_config_sha256: str | None = None,
        runtime_config_sha256: str | None = None,
        preflight_report_sha256: str | None = None,
        factory_binding_sha256: str | None = None,
        resource_topology: str = "INDEPENDENT_GPU_CONCURRENT",
        resource_cleanup_upper_bound_seconds: int = 1,
        resource_cleanup_upper_bound_preimage: bytes | None = None,
        resource_cleanup_upper_bound_sha256: str | None = None,
        max_model_switches: int = 0,
        max_model_switch_wall_time_seconds: int = 0,
        max_total_model_switch_wall_time_seconds: int = 0,
        production_evidence_required: bool = False,
        pilot_switch_authority_sha256: str | None = None,
    ) -> None:
        legacy_defaults = (
            runtime_config_sha256 is None
            and pricing_sha256 is None
            and sentinel_config_sha256 is None
            and preflight_report_sha256 is None
            and factory_binding_sha256 is None
            and resource_cleanup_upper_bound_preimage is None
            and resource_cleanup_upper_bound_sha256 is None
            and not production_evidence_required
        )
        if production_evidence_required and any(
            value is None
            for value in (
                runtime_config_sha256,
                pricing_sha256,
                sentinel_config_sha256,
                preflight_report_sha256,
                factory_binding_sha256,
                resource_cleanup_upper_bound_preimage,
                resource_cleanup_upper_bound_sha256,
            )
        ):
            raise ValueError("production output transaction bindings are incomplete")
        if legacy_defaults:
            runtime_config_sha256 = "0" * 64
            pricing_sha256 = "0" * 64
            sentinel_config_sha256 = "0" * 64
            preflight_report_sha256 = "0" * 64
            factory_binding_sha256 = "0" * 64
            resource_cleanup_upper_bound_preimage = _canonical_bytes(
                {
                    "domain": "legacy-cpu-resource-cleanup-bound",
                    "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
                    "value": {
                        "cleanup_upper_bound_seconds": resource_cleanup_upper_bound_seconds,
                        "resource_topology": resource_topology,
                        "runtime_config_sha256": runtime_config_sha256,
                    },
                }
            )
            resource_cleanup_upper_bound_sha256 = hashlib.sha256(
                resource_cleanup_upper_bound_preimage
            ).hexdigest()
        assert runtime_config_sha256 is not None
        assert pricing_sha256 is not None
        assert sentinel_config_sha256 is not None
        assert preflight_report_sha256 is not None
        assert factory_binding_sha256 is not None
        assert resource_cleanup_upper_bound_preimage is not None
        assert resource_cleanup_upper_bound_sha256 is not None
        for value, name, length in (
            (source_commit, "source_commit", 40),
            (manifest_sha256, "manifest_sha256", 64),
            (runtime_config_sha256, "runtime_config_sha256", 64),
            (pricing_sha256, "pricing_sha256", 64),
            (sentinel_config_sha256, "sentinel_config_sha256", 64),
            (preflight_report_sha256, "preflight_report_sha256", 64),
            (factory_binding_sha256, "factory_binding_sha256", 64),
            (
                resource_cleanup_upper_bound_sha256,
                "resource_cleanup_upper_bound_sha256",
                64,
            ),
        ):
            if not _is_lower_hex(value, length):
                raise ValueError(f"{name} is invalid")
        if type(run_id) is not str or not run_id:
            raise ValueError("transaction run_id is invalid")
        if resource_topology not in {
            "INDEPENDENT_GPU_CONCURRENT",
            "SINGLE_GPU_SEQUENTIAL_SHARED",
        }:
            raise ValueError("transaction resource topology is invalid")
        if (
            type(resource_cleanup_upper_bound_seconds) is not int
            or not 1 <= resource_cleanup_upper_bound_seconds <= 86_400
            or type(resource_cleanup_upper_bound_preimage) is not bytes
            or not resource_cleanup_upper_bound_preimage
            or hashlib.sha256(resource_cleanup_upper_bound_preimage).hexdigest()
            != resource_cleanup_upper_bound_sha256
        ):
            raise ValueError("transaction resource cleanup proof is invalid")
        try:
            cleanup_bound = json.loads(resource_cleanup_upper_bound_preimage.decode("utf-8"))
        except Exception as exc:
            raise ValueError("transaction resource cleanup proof is invalid") from exc
        if (
            type(cleanup_bound) is not dict
            or _canonical_bytes(cleanup_bound) != resource_cleanup_upper_bound_preimage
            or type(cleanup_bound.get("value")) is not dict
            or cleanup_bound["value"].get("cleanup_upper_bound_seconds")
            != resource_cleanup_upper_bound_seconds
            or cleanup_bound["value"].get("resource_topology") != resource_topology
            or cleanup_bound["value"].get("runtime_config_sha256") != runtime_config_sha256
        ):
            raise ValueError("transaction resource cleanup proof binding is invalid")
        for integer_value, name in (
            (max_model_switches, "max_model_switches"),
            (max_model_switch_wall_time_seconds, "max_model_switch_wall_time_seconds"),
            (
                max_total_model_switch_wall_time_seconds,
                "max_total_model_switch_wall_time_seconds",
            ),
        ):
            if type(integer_value) is not int or integer_value < 0:
                raise ValueError(f"{name} is invalid")
        if max_total_model_switch_wall_time_seconds != (
            max_model_switches * max_model_switch_wall_time_seconds
        ):
            raise ValueError("transaction model switch budget is not additive")
        if type(production_evidence_required) is not bool:
            raise ValueError("transaction evidence mode is invalid")
        if pilot_switch_authority_sha256 is not None and not _is_lower_hex(
            pilot_switch_authority_sha256, 64
        ):
            raise ValueError("pilot switch authority hash is invalid")
        repository = repository_root.resolve(strict=True)
        if not repository.is_dir():
            raise ValueError("repository_root must be a directory")
        if not output_root.is_absolute() or output_root.name in {"", ".", ".."}:
            raise ValueError("output_root must be one absolute fresh directory")
        parent, directory_chain = self._open_directory_chain(output_root.parent)
        parent_descriptor = directory_chain[-1][0]
        resolved_output = parent / output_root.name
        if (
            self._entry_exists(parent_descriptor, output_root.name)
            or _is_within(resolved_output, repository)
            or self._entry_exists(
                parent_descriptor,
                f".{output_root.name}.{manifest_sha256[:16]}.partial",
            )
        ):
            self._close_directory_chain(directory_chain)
            raise ValueError(
                "output_root must be fresh, repository-external, and without a stale transaction"
            )
        self._output_root = resolved_output
        self._parent = parent
        self._output_name = output_root.name
        self._staging_name = f".{output_root.name}.{manifest_sha256[:16]}.partial"
        self._staging = parent / self._staging_name
        self._directory_chain = directory_chain
        self._parent_descriptor = parent_descriptor
        self._root_descriptor = -1
        self._root_identity: tuple[int, int] | None = None
        self._binding = {
            "authorized_stages": [stage.value for stage in self._STAGE_FILES],
            "execution_scope": SequenceExecutionScopeV1.R24_R25_FULL.value,
            "factory_binding_sha256": factory_binding_sha256,
            "manifest_sha256": manifest_sha256,
            "max_model_switch_wall_time_seconds": max_model_switch_wall_time_seconds,
            "max_model_switches": max_model_switches,
            "max_total_model_switch_wall_time_seconds": (max_total_model_switch_wall_time_seconds),
            "preflight_report_sha256": preflight_report_sha256,
            "pricing_sha256": pricing_sha256,
            "pilot_switch_authority_sha256": pilot_switch_authority_sha256,
            "production_evidence_required": production_evidence_required,
            "resource_cleanup_upper_bound": cleanup_bound,
            "resource_cleanup_upper_bound_seconds": resource_cleanup_upper_bound_seconds,
            "resource_cleanup_upper_bound_sha256": resource_cleanup_upper_bound_sha256,
            "resource_topology": resource_topology,
            "run_id": run_id,
            "runtime_config_sha256": runtime_config_sha256,
            "sentinel_config_sha256": sentinel_config_sha256,
            "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
            "source_commit": source_commit,
        }
        self._legacy_binding = legacy_defaults
        self._begun = False
        self._committed = False
        self._failed = False
        self._moved_to_output = False
        self._recorded: list[RunStageV1] = []
        self._stage_file_sha256s: dict[RunStageV1, str] = {}
        self._cleanup_evidence_sha256: str | None = None
        self._cleanup_file_sha256: str | None = None
        self._pending_failure_payload: bytes | None = None

    @property
    def committed(self) -> bool:
        return self._committed

    @property
    def output_root(self) -> Path:
        return self._output_root

    def _active_root(self) -> Path:
        return self._output_root if self._moved_to_output else self._staging

    def _verify_directory_chain(self) -> None:
        for index, (descriptor, component, expected_device, expected_inode) in enumerate(
            self._directory_chain
        ):
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                expected_device,
                expected_inode,
            ):
                raise RuntimeError("output parent descriptor identity drifted")
            if index == 0:
                continue
            assert component is not None
            named = os.stat(
                component,
                dir_fd=self._directory_chain[index - 1][0],
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
                expected_device,
                expected_inode,
            ):
                raise RuntimeError("output parent path identity drifted")

    def _verify_root_binding(self) -> None:
        self._verify_directory_chain()
        if self._root_descriptor < 0 or self._root_identity is None:
            raise RuntimeError("output transaction directory is not open")
        opened = os.fstat(self._root_descriptor)
        expected_name = self._output_name if self._moved_to_output else self._staging_name
        named = os.stat(
            expected_name,
            dir_fd=self._parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != self._root_identity
            or (named.st_dev, named.st_ino) != self._root_identity
            or stat.S_IMODE(opened.st_mode) != 0o700
            or opened.st_uid != os.geteuid()
        ):
            raise RuntimeError("output transaction directory identity drifted")

    def _fsync_root(self) -> None:
        self._verify_root_binding()
        os.fsync(self._root_descriptor)

    def _fsync_parent(self) -> None:
        self._verify_directory_chain()
        os.fsync(self._parent_descriptor)

    def _verify_regular_entry(
        self,
        name: str,
        opened: os.stat_result,
    ) -> None:
        named = os.stat(name, dir_fd=self._root_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
        ):
            raise RuntimeError("output evidence file identity differs")

    def _write_once(self, name: str, payload: bytes) -> str:
        if (
            type(name) is not str
            or not name
            or "/" in name
            or name in {".", ".."}
            or type(payload) is not bytes
        ):
            raise RuntimeError("output evidence file name or payload differs")
        self._verify_root_binding()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, 0o600, dir_fd=self._root_descriptor)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("output evidence write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            self._verify_regular_entry(name, opened)
        finally:
            os.close(descriptor)
        self._fsync_root()
        return hashlib.sha256(payload).hexdigest()

    def _write_atomic_terminal(self, name: str, payload: bytes) -> str:
        if type(name) is not str or "/" in name or type(payload) is not bytes:
            raise RuntimeError("full terminal marker name or payload differs")
        self._verify_root_binding()
        temporary = f".{name}.{hashlib.sha256(payload).hexdigest()[:16]}.partial"
        if self._entry_exists(self._root_descriptor, name) or self._entry_exists(
            self._root_descriptor, temporary
        ):
            raise RuntimeError("full terminal marker is not fresh")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=self._root_descriptor)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("terminal write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            self._verify_regular_entry(temporary, opened)
        finally:
            os.close(descriptor)
        os.replace(
            temporary,
            name,
            src_dir_fd=self._root_descriptor,
            dst_dir_fd=self._root_descriptor,
        )
        named = os.stat(name, dir_fd=self._root_descriptor, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("terminal rename identity differs")
        self._fsync_root()
        return hashlib.sha256(payload).hexdigest()

    def _revoke_success_terminal(self, payload: bytes) -> bool:
        if not self._moved_to_output:
            return True
        try:
            self._verify_root_binding()
            if self._entry_exists(self._root_descriptor, "terminal.json"):
                descriptor = os.open(
                    "terminal.json",
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self._root_descriptor,
                )
                try:
                    opened = os.fstat(descriptor)
                    self._verify_regular_entry("terminal.json", opened)
                    with os.fdopen(os.dup(descriptor), "rb") as stream:
                        existing = stream.read(len(payload) + 1)
                finally:
                    os.close(descriptor)
                if existing != payload:
                    return False
                os.unlink("terminal.json", dir_fd=self._root_descriptor)
            for candidate in os.listdir(self._root_descriptor):
                if not (candidate.startswith(".terminal.json.") and candidate.endswith(".partial")):
                    continue
                metadata = os.stat(
                    candidate,
                    dir_fd=self._root_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(metadata.st_mode):
                    return False
                os.unlink(candidate, dir_fd=self._root_descriptor)
            self._fsync_root()
            self._fsync_parent()
            return not self._entry_exists(self._root_descriptor, "terminal.json")
        except Exception:
            return False

    def _rename_root_to_output(self) -> None:
        if self._moved_to_output:
            self._verify_root_binding()
            return
        self._verify_root_binding()
        if self._entry_exists(self._parent_descriptor, self._output_name):
            raise RuntimeError("output destination is no longer fresh")
        self._fsync_root()
        os.replace(
            self._staging_name,
            self._output_name,
            src_dir_fd=self._parent_descriptor,
            dst_dir_fd=self._parent_descriptor,
        )
        self._moved_to_output = True
        self._verify_root_binding()
        self._fsync_parent()

    def begin(self) -> None:
        if self._committed:
            raise RuntimeError("output transaction is already committed")
        if self._begun:
            return
        self._verify_directory_chain()
        if self._entry_exists(self._parent_descriptor, self._staging_name) or self._entry_exists(
            self._parent_descriptor, self._output_name
        ):
            raise RuntimeError("output transaction target is no longer fresh")
        os.mkdir(self._staging_name, mode=0o700, dir_fd=self._parent_descriptor)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        self._root_descriptor = os.open(
            self._staging_name,
            flags,
            dir_fd=self._parent_descriptor,
        )
        os.fchmod(self._root_descriptor, 0o700)
        root_metadata = os.fstat(self._root_descriptor)
        self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        self._fsync_root()
        self._fsync_parent()
        self._begun = True
        try:
            self._write_once("manifest-binding.json", _canonical_bytes(self._binding))
        except Exception:
            self.rollback()
            raise

    def record(self, receipt: StageExecutionReceiptV1, evidence_preimage: bytes) -> None:
        if not self._begun or self._committed or self._failed:
            raise RuntimeError("output transaction is not writable")
        expected_index = len(self._recorded)
        expected_stage = tuple(self._STAGE_FILES)[expected_index]
        if receipt.stage is not expected_stage:
            raise RuntimeError("output stages are out of order")
        if (
            type(evidence_preimage) is not bytes
            or hashlib.sha256(evidence_preimage).hexdigest() != receipt.evidence_sha256
        ):
            raise RuntimeError("stage evidence preimage differs from receipt")
        try:
            evidence = json.loads(evidence_preimage.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("stage evidence preimage is invalid") from exc
        if type(evidence) is not dict or _canonical_bytes(evidence) != evidence_preimage:
            raise RuntimeError("stage evidence preimage is not canonical")
        payload = {
            **self._binding,
            "evidence": evidence,
            "receipt": _receipt_projection(receipt),
        }
        file_sha256 = self._write_once(self._STAGE_FILES[receipt.stage], _canonical_bytes(payload))
        self._recorded.append(receipt.stage)
        self._stage_file_sha256s[receipt.stage] = file_sha256

    def record_cleanup_success(self, evidence_preimage: bytes) -> str:
        if (
            not self._begun
            or self._committed
            or self._failed
            or self._moved_to_output
            or tuple(self._recorded) != tuple(self._STAGE_FILES)
            or self._cleanup_file_sha256 is not None
        ):
            raise RuntimeError("cleanup proof is outside the full terminal boundary")
        if type(evidence_preimage) is not bytes:
            raise RuntimeError("cleanup evidence type differs")
        try:
            evidence = json.loads(evidence_preimage.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("cleanup evidence is invalid") from exc
        if type(evidence) is not dict or _canonical_bytes(evidence) != evidence_preimage:
            raise RuntimeError("cleanup evidence is not canonical")
        evidence_sha256 = hashlib.sha256(evidence_preimage).hexdigest()
        if self._binding["production_evidence_required"]:
            if evidence.get("domain") != "production-resource-cleanup-evidence":
                raise RuntimeError("production cleanup evidence domain differs")
            value = evidence.get("value")
            if type(value) is not dict:
                raise RuntimeError("production cleanup evidence value differs")
            if (
                value.get("manifest_sha256") != self._binding["manifest_sha256"]
                or value.get("runtime_config_sha256") != self._binding["runtime_config_sha256"]
                or value.get("resource_topology") != self._binding["resource_topology"]
                or value.get("sequence_scope_authority_sha256") != self._binding["manifest_sha256"]
                or value.get("sequence_execution_scope") != "R24_R25_FULL"
                or value.get("status") != "CLEANED"
            ):
                raise RuntimeError("production cleanup evidence root binding differs")
        if (
            self._binding["production_evidence_required"]
            and self._binding["resource_topology"] == "SINGLE_GPU_SEQUENTIAL_SHARED"
        ):
            assert type(value) is dict
            expected_pilot_switches = cast(int, self._binding["max_model_switches"]) - 1
            switch_authority_sha256: str | None = None
            if (
                type(value.get("pilot_model_switch_evidence")) is not list
                or len(value["pilot_model_switch_evidence"]) != expected_pilot_switches
                or type(value.get("pilot_model_switch_evidence_sha256s")) is not list
                or len(value["pilot_model_switch_evidence_sha256s"]) != expected_pilot_switches
                or value.get("unconsumed_pilot_model_switch_evidence_sha256s") != []
                or any(
                    type(item) is not dict
                    or not _is_lower_hex(digest, 64)
                    or hashlib.sha256(_canonical_bytes(item)).hexdigest() != digest
                    for item, digest in zip(
                        value["pilot_model_switch_evidence"],
                        value["pilot_model_switch_evidence_sha256s"],
                        strict=True,
                    )
                )
            ):
                raise RuntimeError("shared cleanup switch evidence is incomplete")
            for index, (item, digest) in enumerate(
                zip(
                    value["pilot_model_switch_evidence"],
                    value["pilot_model_switch_evidence_sha256s"],
                    strict=True,
                )
            ):
                assert type(item) is dict
                item_value = item.get("value")
                transition = item_value.get("transition") if type(item_value) is dict else None
                expected_target = "QWEN3_VL" if index % 2 == 0 else "MAI_UI"
                expected_source = "MAI_UI" if index % 2 == 0 else "QWEN3_VL"
                if (
                    item.get("domain") != "production-pilot-model-switch-evidence"
                    or type(item_value) is not dict
                    or item_value.get("switch_index") != index
                    or item_value.get("pilot_host_block_count") != expected_pilot_switches
                    or item_value.get("max_model_switches") != self._binding["max_model_switches"]
                    or item_value.get("max_model_switch_wall_time_seconds")
                    != self._binding["max_model_switch_wall_time_seconds"]
                    or item_value.get("max_total_model_switch_wall_time_seconds")
                    != self._binding["max_total_model_switch_wall_time_seconds"]
                    or type(transition) is not dict
                    or transition.get("manifest_sha256") != self._binding["manifest_sha256"]
                    or transition.get("runtime_config_sha256")
                    != self._binding["runtime_config_sha256"]
                    or transition.get("sequence_execution_scope") != "R24_R25_FULL"
                    or transition.get("sequence_scope_authority_sha256")
                    != self._binding["manifest_sha256"]
                    or transition.get("resource_topology") != "SINGLE_GPU_SEQUENTIAL_SHARED"
                    or transition.get("source_host") != expected_source
                    or transition.get("target_host") != expected_target
                    or not _is_lower_hex(digest, 64)
                ):
                    raise RuntimeError("shared cleanup switch evidence order/root differs")
                current_authority = item_value.get("switch_authority_sha256")
                if not _is_lower_hex(current_authority, 64):
                    raise RuntimeError("shared cleanup switch authority hash differs")
                if switch_authority_sha256 is None:
                    switch_authority_sha256 = cast(str, current_authority)
                elif switch_authority_sha256 != current_authority:
                    raise RuntimeError("shared cleanup switch authority root drifted")
            if switch_authority_sha256 != self._binding["pilot_switch_authority_sha256"]:
                raise RuntimeError("shared cleanup switch authority binding differs")
        elif self._binding["production_evidence_required"]:
            concurrent_value = cast(dict[str, object], value)
            if (
                concurrent_value.get("pilot_model_switch_evidence") is not None
                or concurrent_value.get("pilot_model_switch_evidence_sha256s") is not None
            ):
                raise RuntimeError("concurrent cleanup cannot contain model switch evidence")
        payload = _canonical_bytes(
            {
                **self._binding,
                "resource_cleanup_evidence": evidence,
                "resource_cleanup_evidence_sha256": evidence_sha256,
                "resource_cleanup_status": "SUCCEEDED",
            }
        )
        self._cleanup_file_sha256 = self._write_once("04-resource-cleanup.json", payload)
        self._cleanup_evidence_sha256 = evidence_sha256
        return evidence_sha256

    def fail(
        self,
        *,
        failed_stage: RunStageV1,
        failure_code: str,
        stage_failure_evidence_preimage: bytes | None,
        resource_cleanup_evidence_preimage: bytes | None = None,
        resource_cleanup_failure_code: str | None = None,
        resource_cleanup_status: str = "SUCCEEDED",
        cleanup_wall_time_ms: int = 0,
        sequence_wall_time_ms: int = 0,
    ) -> None:
        """Atomically publish an incomplete run as failure evidence, never success."""

        if self._committed or self._failed:
            raise RuntimeError("output transaction is already terminal")
        if not self._begun:
            self.begin()
        if type(failed_stage) is not RunStageV1 or type(failure_code) is not str:
            raise RuntimeError("failure evidence is invalid")
        stage_failure_evidence: object = None
        stage_failure_evidence_sha256: str | None = None
        if stage_failure_evidence_preimage is not None:
            try:
                stage_failure_evidence = json.loads(stage_failure_evidence_preimage.decode("utf-8"))
            except Exception as exc:
                raise RuntimeError("stage failure evidence is invalid") from exc
            if (
                type(stage_failure_evidence) is not dict
                or _canonical_bytes(stage_failure_evidence) != stage_failure_evidence_preimage
            ):
                raise RuntimeError("stage failure evidence is not canonical")
            stage_failure_evidence_sha256 = hashlib.sha256(
                stage_failure_evidence_preimage
            ).hexdigest()
        resource_cleanup_evidence: object = None
        resource_cleanup_evidence_sha256: str | None = None
        if resource_cleanup_evidence_preimage is not None:
            try:
                resource_cleanup_evidence = json.loads(
                    resource_cleanup_evidence_preimage.decode("utf-8")
                )
            except Exception as exc:
                raise RuntimeError("resource cleanup evidence is invalid") from exc
            if (
                type(resource_cleanup_evidence) is not dict
                or _canonical_bytes(resource_cleanup_evidence) != resource_cleanup_evidence_preimage
            ):
                raise RuntimeError("resource cleanup evidence is not canonical")
            resource_cleanup_evidence_sha256 = hashlib.sha256(
                resource_cleanup_evidence_preimage
            ).hexdigest()
        if self._legacy_binding:
            if (
                resource_cleanup_evidence is not None
                or resource_cleanup_failure_code is not None
                or resource_cleanup_status != "SUCCEEDED"
            ):
                raise RuntimeError("legacy resource cleanup status is invalid")
        elif (
            resource_cleanup_status
            not in {
                "SUCCEEDED",
                "RETRY_REQUIRED",
                "OWNED_RESOURCES_CLEANED_TENANT_DRIFT",
            }
            or resource_cleanup_evidence is None
            or (
                resource_cleanup_status
                in {"RETRY_REQUIRED", "OWNED_RESOURCES_CLEANED_TENANT_DRIFT"}
                and resource_cleanup_failure_code is None
            )
            or (
                resource_cleanup_status == "SUCCEEDED" and resource_cleanup_failure_code is not None
            )
        ):
            raise RuntimeError("resource cleanup status is invalid")
        if (
            type(cleanup_wall_time_ms) is not int
            or cleanup_wall_time_ms < 0
            or type(sequence_wall_time_ms) is not int
            or sequence_wall_time_ms < cleanup_wall_time_ms
        ):
            raise RuntimeError("failure wall census is invalid")
        payload = _canonical_bytes(
            {
                **self._binding,
                "completed_stages": [item.value for item in self._recorded],
                "failed_stage": failed_stage.value,
                "failure_code": failure_code,
                "resource_cleanup_evidence": resource_cleanup_evidence,
                "resource_cleanup_evidence_sha256": resource_cleanup_evidence_sha256,
                "resource_cleanup_failure_code": resource_cleanup_failure_code,
                "resource_cleanup_status": resource_cleanup_status,
                "resource_cleanup_wall_time_ms": cleanup_wall_time_ms,
                "sequence_wall_time_ms": sequence_wall_time_ms,
                "stage_failure_evidence": stage_failure_evidence,
                "stage_failure_evidence_sha256": stage_failure_evidence_sha256,
                "stage_file_sha256s": {
                    stage.value: digest for stage, digest in self._stage_file_sha256s.items()
                },
                "status": "FAILED",
                "terminal_output_published": True,
            }
        )
        self._pending_failure_payload = payload
        self._write_atomic_terminal("failure.json", payload)
        if not self._moved_to_output:
            self._rename_root_to_output()
        self._fsync_parent()
        self._failed = True

    def preserve_failure_recovery(
        self,
        *,
        failed_stage: RunStageV1,
        failure_code: str,
    ) -> None:
        """Durably mark an unpublishable failure without deleting prior evidence."""

        if self._root_descriptor < 0 or self._root_identity is None:
            raise RuntimeError("failure recovery directory is unavailable")
        self._verify_root_binding()
        payload = _canonical_bytes(
            {
                **self._binding,
                "completed_stages": [item.value for item in self._recorded],
                "failure_envelope": (
                    None
                    if self._pending_failure_payload is None
                    else json.loads(self._pending_failure_payload)
                ),
                "failure_envelope_sha256": (
                    None
                    if self._pending_failure_payload is None
                    else hashlib.sha256(self._pending_failure_payload).hexdigest()
                ),
                "failed_stage": failed_stage.value,
                "failure_code": failure_code,
                "status": "FAILURE_PUBLICATION_INCOMPLETE",
            }
        )
        if self._entry_exists(self._root_descriptor, "recovery.json"):
            descriptor = os.open(
                "recovery.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_descriptor,
            )
            try:
                metadata = os.fstat(descriptor)
                self._verify_regular_entry("recovery.json", metadata)
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    existing = stream.read(len(payload) + 1)
            finally:
                os.close(descriptor)
            if existing != payload:
                raise RuntimeError("failure recovery marker differs")
        else:
            self._write_once("recovery.json", payload)
        self._fsync_root()
        self._fsync_parent()

    def commit(
        self,
        result: SequenceRunResultV1,
        *,
        census: dict[str, object],
        publication_deadline_monotonic_ns: int | None = None,
    ) -> None:
        if (
            not self._begun
            or self._committed
            or self._failed
            or tuple(self._recorded) != tuple(self._STAGE_FILES)
            or self._cleanup_file_sha256 is None
            or self._cleanup_evidence_sha256 is None
            or type(result) is not SequenceRunResultV1
            or result.status is not SequenceStatusV1.COMPLETE
            or result.receipts != tuple(cast(list[StageExecutionReceiptV1], census["receipts"]))
            or self._entry_exists(self._parent_descriptor, self._output_name)
            or (
                publication_deadline_monotonic_ns is not None
                and (
                    type(publication_deadline_monotonic_ns) is not int
                    or time.monotonic_ns() >= publication_deadline_monotonic_ns
                )
            )
        ):
            raise RuntimeError("output transaction is incomplete or no longer fresh")
        census_projection = {
            "actor_actions": census["actor_actions"],
            "actor_calls": census["actor_calls"],
            "cleanup_attempted": True,
            "cleanup_evidence_sha256": self._cleanup_evidence_sha256,
            "cleanup_succeeded": True,
            "cleanup_wall_time_ms": census["cleanup_wall_time_ms"],
            "completed_stages": [stage.value for stage in self._recorded],
            "cost_usd_micros": census["cost_usd_micros"],
            "openai_calls": census["openai_calls"],
            "output_committed": True,
            "secret_leases_acquired": census["secret_leases_acquired"],
            "secret_leases_closed": census["secret_leases_closed"],
            "stage_wall_time_ms": census["stage_wall_time_ms"],
            "state": ExecutorStateV1.COMPLETE.value,
            "wall_time_ms": census["wall_time_ms"],
        }
        expected_handoff_count = (
            1 if self._binding["resource_topology"] == "SINGLE_GPU_SEQUENTIAL_SHARED" else 0
        )
        handoff_evidence_sha256 = census["handoff_evidence_sha256"]
        if (
            type(census["cleanup_wall_time_ms"]) is not int
            or census["cleanup_wall_time_ms"] < 0
            or type(census["stage_wall_time_ms"]) is not int
            or census["stage_wall_time_ms"] < 0
            or type(census["wall_time_ms"]) is not int
            or census["wall_time_ms"]
            < max(
                census["stage_wall_time_ms"],
                census["cleanup_wall_time_ms"],
            )
            or (expected_handoff_count == 1 and not _is_lower_hex(handoff_evidence_sha256, 64))
            or (expected_handoff_count == 0 and handoff_evidence_sha256 is not None)
        ):
            raise RuntimeError("terminal cleanup/handoff census differs")
        result_projection = _sequence_result_projection(result)
        terminal_payload = _canonical_bytes(
            {
                **self._binding,
                "acceptance_status": "EXECUTION_COMPLETE_INTEGRITY_PENDING",
                "cleanup_file_sha256": self._cleanup_file_sha256,
                "executor_census": census_projection,
                "executor_census_sha256": hashlib.sha256(
                    _canonical_bytes(census_projection)
                ).hexdigest(),
                "handoff_model_switch_count": (expected_handoff_count),
                "handoff_model_switch_evidence_sha256": handoff_evidence_sha256,
                "pilot_model_switch_count": (
                    cast(int, self._binding["max_model_switches"]) - 1
                    if self._binding["resource_topology"] == "SINGLE_GPU_SEQUENTIAL_SHARED"
                    else 0
                ),
                "result": result_projection,
                "result_sha256": hashlib.sha256(_canonical_bytes(result_projection)).hexdigest(),
                "stage_file_sha256s": {
                    stage.value: self._stage_file_sha256s[stage] for stage in self._STAGE_FILES
                },
                "status": "COMPLETE",
                "terminal_output_published": True,
                "total_model_switch_count": self._binding["max_model_switches"],
            }
        )
        try:
            self._rename_root_to_output()
            self._write_atomic_terminal("terminal.json", terminal_payload)
            self._fsync_parent()
            if (
                publication_deadline_monotonic_ns is not None
                and time.monotonic_ns() >= publication_deadline_monotonic_ns
            ):
                raise RuntimeError("terminal publication consumed the integrity reserve")
        except Exception:
            if not self._revoke_success_terminal(terminal_payload):
                self._pending_failure_payload = _canonical_bytes(
                    {
                        **self._binding,
                        "failure_code": "SUCCESS_TERMINAL_REVOCATION_UNCONFIRMED",
                        "status": "FAILURE_PUBLICATION_INCOMPLETE",
                    }
                )
            raise
        self._committed = True

    def rollback(self) -> None:
        if self._committed or self._failed or self._moved_to_output:
            return
        if self._root_descriptor >= 0:
            self._verify_root_binding()
            for name in os.listdir(self._root_descriptor):
                metadata = os.stat(
                    name,
                    dir_fd=self._root_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("rollback preserves an unexpected output transaction entry")
                os.unlink(name, dir_fd=self._root_descriptor)
            self._fsync_root()
            os.close(self._root_descriptor)
            self._root_descriptor = -1
            self._root_identity = None
            os.rmdir(self._staging_name, dir_fd=self._parent_descriptor)
        self._fsync_parent()
        self._begun = False
        self._recorded.clear()
        self._stage_file_sha256s.clear()
        self._cleanup_evidence_sha256 = None
        self._cleanup_file_sha256 = None
        self._pending_failure_payload = None

    def __del__(self) -> None:
        root_descriptor = getattr(self, "_root_descriptor", -1)
        if type(root_descriptor) is int and root_descriptor >= 0:
            try:
                os.close(root_descriptor)
            except OSError:
                pass
            self._root_descriptor = -1
        chain = getattr(self, "_directory_chain", ())
        if type(chain) is tuple:
            self._close_directory_chain(chain)
            self._directory_chain = ()


class AtomicR24SmokeOutputTransactionV1:
    """Owner-only output transaction for the R2.4 smoke-only sequence.

    Successful cleanup is written and fsynced before the MAI receipt and the
    final COMPLETE marker can be published.  A directory without a canonical
    ``terminal.json`` is never a successful run.
    """

    _STAGE_FILES: Final[dict[RunStageV1, str]] = {
        RunStageV1.RESOURCE_PREFLIGHT: "00-resource-preflight.json",
        RunStageV1.QWEN_LIVE_SMOKE: "01-qwen-live-smoke.json",
        RunStageV1.MAI_LIVE_SMOKE: "02-mai-live-smoke.json",
    }
    _RECOVERY_BINDING_KEYS: Final[tuple[str, ...]] = (
        "authorized_stages",
        "execution_scope",
        "factory_binding_sha256",
        "manifest_sha256",
        "pilot_executed",
        "preflight_report_sha256",
        "run_id",
        "runtime_config_sha256",
        "schema_version",
        "source_commit",
        "resource_cleanup_upper_bound",
        "resource_cleanup_upper_bound_seconds",
        "resource_cleanup_upper_bound_sha256",
    )

    def __init__(
        self,
        *,
        output_root: Path,
        repository_root: Path,
        run_id: str,
        source_commit: str,
        manifest_sha256: str,
        runtime_config_sha256: str,
        preflight_report_sha256: str,
        factory_binding_sha256: str,
        resource_cleanup_upper_bound_seconds: int,
        resource_cleanup_upper_bound_preimage: bytes,
        resource_cleanup_upper_bound_sha256: str,
    ) -> None:
        for value, name, length in (
            (source_commit, "source_commit", 40),
            (manifest_sha256, "manifest_sha256", 64),
            (runtime_config_sha256, "runtime_config_sha256", 64),
            (preflight_report_sha256, "preflight_report_sha256", 64),
            (factory_binding_sha256, "factory_binding_sha256", 64),
            (
                resource_cleanup_upper_bound_sha256,
                "resource_cleanup_upper_bound_sha256",
                64,
            ),
        ):
            if not _is_lower_hex(value, length):
                raise ValueError(f"{name} is invalid")
        if type(run_id) is not str or not run_id:
            raise ValueError("transaction run_id is invalid")
        if (
            type(resource_cleanup_upper_bound_seconds) is not int
            or resource_cleanup_upper_bound_seconds < R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS
            or type(resource_cleanup_upper_bound_preimage) is not bytes
            or hashlib.sha256(resource_cleanup_upper_bound_preimage).hexdigest()
            != resource_cleanup_upper_bound_sha256
        ):
            raise ValueError("resource cleanup upper bound proof is invalid")
        resource_cleanup_upper_bound = self._canonical_object(
            resource_cleanup_upper_bound_preimage,
            "resource cleanup upper bound",
        )
        repository = repository_root.resolve(strict=True)
        parent = output_root.parent.resolve(strict=True)
        resolved_output = output_root.resolve(strict=False)
        if (
            not repository.is_dir()
            or not parent.is_dir()
            or output_root.exists()
            or output_root.is_symlink()
            or _is_within(resolved_output, repository)
        ):
            raise ValueError("smoke output must be fresh and repository-external")
        self._output_root = resolved_output
        self._parent = parent
        self._staging = parent / f".{output_root.name}.{manifest_sha256[:16]}.smoke-partial"
        if self._staging.exists() or self._staging.is_symlink():
            raise ValueError("stale smoke output transaction exists")
        self._binding: dict[str, object] = {
            "authorized_stages": [stage.value for stage in self._STAGE_FILES],
            "execution_scope": SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY.value,
            "factory_binding_sha256": factory_binding_sha256,
            "manifest_sha256": manifest_sha256,
            "pilot_executed": False,
            "preflight_report_sha256": preflight_report_sha256,
            "run_id": run_id,
            "runtime_config_sha256": runtime_config_sha256,
            "resource_cleanup_upper_bound": resource_cleanup_upper_bound,
            "resource_cleanup_upper_bound_seconds": (resource_cleanup_upper_bound_seconds),
            "resource_cleanup_upper_bound_sha256": (resource_cleanup_upper_bound_sha256),
            "schema_version": R24_SMOKE_EXECUTOR_BINDING_SCHEMA_VERSION,
            "source_commit": source_commit,
        }
        self._begun = False
        self._committed = False
        self._failed = False
        self._moved_to_output = False
        self._recorded: list[RunStageV1] = []
        self._stage_file_sha256s: dict[RunStageV1, str] = {}
        self._cleanup_evidence_sha256: str | None = None
        self._cleanup_file_sha256: str | None = None
        self._success_terminal_revocation_unconfirmed = False
        self._pending_failure_payload: bytes | None = None

    @property
    def committed(self) -> bool:
        return self._committed

    @property
    def failure_published(self) -> bool:
        return self._failed

    @property
    def output_root(self) -> Path:
        return self._output_root

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("smoke output directory changed type")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
        if type(raw) is not bytes or not raw or len(raw) > 64 * 1024 * 1024:
            raise RuntimeError(f"{label} evidence bytes are invalid")
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"{label} evidence is not UTF-8 JSON") from exc
        if type(value) is not dict or _canonical_bytes(value) != raw:
            raise RuntimeError(f"{label} evidence is not a canonical object")
        return value

    def _active_root(self) -> Path:
        if self._moved_to_output:
            return self._output_root
        return self._staging

    def _write_once(self, name: str, payload: bytes) -> str:
        root = self._active_root()
        target = root / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        self._fsync_directory(root)
        return hashlib.sha256(payload).hexdigest()

    def _write_atomic_terminal(self, name: str, payload: bytes) -> str:
        root = self._active_root()
        target = root / name
        temporary = root / f".{name}.{hashlib.sha256(payload).hexdigest()[:16]}.partial"
        if target.exists() or target.is_symlink() or temporary.exists() or temporary.is_symlink():
            raise RuntimeError("smoke terminal marker is not fresh")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        self._fsync_directory(root)
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _lstat_if_present(path: Path) -> os.stat_result | None:
        try:
            return path.lstat()
        except FileNotFoundError:
            return None

    @staticmethod
    def _read_exact_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size < 1
                or metadata.st_size > maximum_bytes
            ):
                raise RuntimeError("smoke terminal marker is not a bounded regular file")
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise RuntimeError("smoke terminal marker was truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise RuntimeError("smoke terminal marker grew while being read")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _revoke_success_terminal_after_commit_error(self, terminal_payload: bytes) -> bool:
        """Remove a visible COMPLETE marker before ordinary failure publication.

        Returning ``False`` means that absence/durability could not be confirmed.
        The caller must then publish only a recovery marker; ``fail`` enforces this
        one-way state and cannot create a competing ordinary failure terminal.
        """

        self._success_terminal_revocation_unconfirmed = True
        root = self._output_root
        terminal = root / "terminal.json"
        temporary = root / (
            f".terminal.json.{hashlib.sha256(terminal_payload).hexdigest()[:16]}.partial"
        )
        try:
            terminal_metadata = self._lstat_if_present(terminal)
            if terminal_metadata is not None:
                if not stat.S_ISREG(terminal_metadata.st_mode):
                    return False
                if (
                    self._read_exact_regular_file(
                        terminal,
                        maximum_bytes=64 * 1024 * 1024,
                    )
                    != terminal_payload
                ):
                    return False
                terminal.unlink()

            temporary_metadata = self._lstat_if_present(temporary)
            if temporary_metadata is not None:
                if not stat.S_ISREG(temporary_metadata.st_mode):
                    return False
                temporary.unlink()

            self._fsync_directory(root)
            self._fsync_directory(self._parent)
            if self._lstat_if_present(terminal) is not None:
                return False
            self._success_terminal_revocation_unconfirmed = False
            return True
        except Exception:
            return False

    def _assert_failure_marker_is_unambiguous(self) -> None:
        if self._success_terminal_revocation_unconfirmed:
            raise RuntimeError("successful terminal revocation is unconfirmed")
        root = self._active_root()
        for name in ("terminal.json", "recovery.json"):
            if self._lstat_if_present(root / name) is not None:
                raise RuntimeError("smoke output already has a competing terminal marker")

    @classmethod
    def _validate_failed_terminal_envelope(cls, value: dict[str, object]) -> None:
        expected_keys = set(cls._RECOVERY_BINDING_KEYS) | {
            "resource_cleanup_evidence",
            "resource_cleanup_evidence_sha256",
            "result",
            "result_sha256",
            "stage_failure_evidence",
            "stage_failure_evidence_sha256",
            "stage_file_sha256s",
            "status",
        }
        result = value.get("result")
        cleanup = value.get("resource_cleanup_evidence")
        failure_evidence = value.get("stage_failure_evidence")
        stage_file_sha256s = value.get("stage_file_sha256s")
        if (
            set(value) != expected_keys
            or value.get("status") != "FAILED"
            or type(result) is not dict
            or not _is_lower_hex(value.get("result_sha256"), 64)
            or hashlib.sha256(_canonical_bytes(result)).hexdigest() != value["result_sha256"]
            or result.get("status") != "FAILED"
            or result.get("successful_output_committed") is not False
            or result.get("terminal_output_published") is not True
            or type(cleanup) is not dict
            or not _is_lower_hex(value.get("resource_cleanup_evidence_sha256"), 64)
            or hashlib.sha256(_canonical_bytes(cleanup)).hexdigest()
            != value["resource_cleanup_evidence_sha256"]
            or result.get("resource_cleanup_evidence_sha256")
            != value["resource_cleanup_evidence_sha256"]
            or type(stage_file_sha256s) is not dict
            or any(
                key not in {stage.value for stage in cls._STAGE_FILES}
                or not _is_lower_hex(digest, 64)
                for key, digest in stage_file_sha256s.items()
            )
            or any(
                result.get(key) != value.get(key)
                for key in (
                    "execution_scope",
                    "factory_binding_sha256",
                    "manifest_sha256",
                    "pilot_executed",
                    "preflight_report_sha256",
                    "run_id",
                    "resource_cleanup_upper_bound",
                    "resource_cleanup_upper_bound_seconds",
                    "resource_cleanup_upper_bound_sha256",
                    "runtime_config_sha256",
                    "source_commit",
                )
            )
        ):
            raise RuntimeError("smoke failed terminal envelope is invalid")
        failure_sha256 = value.get("stage_failure_evidence_sha256")
        if failure_evidence is None:
            if failure_sha256 is not None:
                raise RuntimeError("smoke failed terminal evidence hash is invalid")
        elif (
            type(failure_evidence) is not dict
            or not _is_lower_hex(failure_sha256, 64)
            or hashlib.sha256(_canonical_bytes(failure_evidence)).hexdigest() != failure_sha256
        ):
            raise RuntimeError("smoke failed terminal evidence proof is invalid")

    @classmethod
    def read_terminal_marker(cls, output_root: Path) -> dict[str, object]:
        """Read exactly one closed terminal marker and reject ambiguous outputs."""

        raw_output_root: object = output_root
        if not isinstance(raw_output_root, Path) or output_root.is_symlink():
            raise RuntimeError("smoke output root is invalid")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_descriptor = os.open(output_root, flags)
        try:
            root_metadata = os.fstat(root_descriptor)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise RuntimeError("smoke output root is not a directory")
            markers: list[str] = []
            for name in ("terminal.json", "failure.json", "recovery.json"):
                try:
                    metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError("smoke terminal marker is not a regular file")
                markers.append(name)
            if len(markers) != 1:
                raise RuntimeError("smoke output has ambiguous terminal markers")
            marker_name = markers[0]
            marker_descriptor = os.open(
                marker_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            try:
                metadata = os.fstat(marker_descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size < 1
                    or metadata.st_size > 64 * 1024 * 1024
                ):
                    raise RuntimeError("smoke terminal marker is invalid")
                chunks: list[bytes] = []
                remaining = metadata.st_size
                while remaining:
                    chunk = os.read(marker_descriptor, min(remaining, 1024 * 1024))
                    if not chunk:
                        raise RuntimeError("smoke terminal marker was truncated")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(marker_descriptor, 1):
                    raise RuntimeError("smoke terminal marker grew while being read")
            finally:
                os.close(marker_descriptor)
        finally:
            os.close(root_descriptor)
        raw = b"".join(chunks)
        value = cls._canonical_object(raw, "terminal")
        expected_status = {
            "terminal.json": "COMPLETE",
            "failure.json": "FAILED",
            "recovery.json": None,
        }[marker_name]
        if (
            value.get("schema_version") != R24_SMOKE_EXECUTOR_BINDING_SCHEMA_VERSION
            or value.get("execution_scope") != SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY.value
            or value.get("pilot_executed") is not False
            or (expected_status is not None and value.get("status") != expected_status)
            or (
                marker_name == "recovery.json"
                and value.get("status")
                not in {
                    "FAILURE_PUBLICATION_INCOMPLETE",
                    "SUCCESS_TERMINAL_REVOCATION_UNCONFIRMED",
                }
            )
        ):
            raise RuntimeError("smoke terminal marker binding is invalid")
        cleanup_upper_bound = value.get("resource_cleanup_upper_bound")
        cleanup_upper_bound_seconds = value.get("resource_cleanup_upper_bound_seconds")
        if (
            type(cleanup_upper_bound) is not dict
            or type(cleanup_upper_bound_seconds) is not int
            or cleanup_upper_bound_seconds < R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS
            or not _is_lower_hex(value.get("resource_cleanup_upper_bound_sha256"), 64)
            or hashlib.sha256(_canonical_bytes(cleanup_upper_bound)).hexdigest()
            != value["resource_cleanup_upper_bound_sha256"]
            or type(cleanup_upper_bound.get("value")) is not dict
            or cleanup_upper_bound["value"].get("cleanup_upper_bound_seconds")
            != cleanup_upper_bound_seconds
        ):
            raise RuntimeError("smoke terminal cleanup bound proof is invalid")
        if marker_name in {"terminal.json", "failure.json"}:
            result = value.get("result")
            if (
                type(result) is not dict
                or not _is_lower_hex(value.get("result_sha256"), 64)
                or hashlib.sha256(_canonical_bytes(result)).hexdigest() != value["result_sha256"]
                or result.get("status") != expected_status
                or result.get("resource_cleanup_upper_bound_seconds") != cleanup_upper_bound_seconds
                or result.get("resource_cleanup_upper_bound") != cleanup_upper_bound
                or result.get("resource_cleanup_upper_bound_sha256")
                != value["resource_cleanup_upper_bound_sha256"]
            ):
                raise RuntimeError("smoke terminal result proof is invalid")
            if marker_name == "failure.json":
                cls._validate_failed_terminal_envelope(value)
        else:
            failure_envelope = value.get("failure_envelope")
            if (
                set(value)
                != set(cls._RECOVERY_BINDING_KEYS)
                | {
                    "failure_code",
                    "failure_envelope",
                    "failure_envelope_sha256",
                    "failure_marker_observed_before_recovery",
                    "status",
                    "terminal_marker_observed_before_recovery",
                }
                or type(failure_envelope) is not dict
                or failure_envelope.get("status") != "FAILED"
                or not _is_lower_hex(value.get("failure_envelope_sha256"), 64)
                or hashlib.sha256(_canonical_bytes(failure_envelope)).hexdigest()
                != value["failure_envelope_sha256"]
                or any(
                    failure_envelope.get(key) != value.get(key)
                    for key in cls._RECOVERY_BINDING_KEYS
                )
                or type(value.get("failure_marker_observed_before_recovery")) is not bool
                or type(value.get("terminal_marker_observed_before_recovery")) is not bool
            ):
                raise RuntimeError("smoke recovery failure proof is invalid")
            cls._validate_failed_terminal_envelope(failure_envelope)
            recovery_result = failure_envelope["result"]
            assert type(recovery_result) is dict
            if value.get("failure_code") != recovery_result.get("failure_code"):
                raise RuntimeError("smoke recovery failure code is invalid")
        return value

    def begin(self) -> None:
        if self._committed or self._failed:
            raise RuntimeError("smoke output transaction is terminal")
        if self._begun:
            return
        os.mkdir(self._staging, mode=0o700)
        os.chmod(self._staging, 0o700)
        self._begun = True
        try:
            self._fsync_directory(self._staging)
            self._fsync_directory(self._parent)
            self._write_once("manifest-binding.json", _canonical_bytes(self._binding))
        except Exception:
            self.rollback()
            raise

    def record(self, receipt: StageExecutionReceiptV1, evidence_preimage: bytes) -> None:
        if not self._begun or self._committed or self._failed or self._moved_to_output:
            raise RuntimeError("smoke output transaction is not writable")
        if type(receipt) is not StageExecutionReceiptV1:
            raise RuntimeError("smoke stage receipt type differs")
        expected_stage = tuple(self._STAGE_FILES)[len(self._recorded)]
        if receipt.stage is not expected_stage:
            raise RuntimeError("smoke output stages are out of order")
        if receipt.stage is RunStageV1.MAI_LIVE_SMOKE and self._cleanup_file_sha256 is None:
            raise RuntimeError("MAI cannot be recorded before successful cleanup proof")
        evidence = self._canonical_object(evidence_preimage, "stage")
        if hashlib.sha256(evidence_preimage).hexdigest() != receipt.evidence_sha256:
            raise RuntimeError("stage evidence preimage differs from receipt")
        payload = _canonical_bytes(
            {
                **self._binding,
                "evidence": evidence,
                "evidence_sha256": receipt.evidence_sha256,
                "receipt": _receipt_projection(receipt),
            }
        )
        file_sha256 = self._write_once(self._STAGE_FILES[receipt.stage], payload)
        self._recorded.append(receipt.stage)
        self._stage_file_sha256s[receipt.stage] = file_sha256

    def record_cleanup_success(self, evidence_preimage: bytes) -> str:
        if (
            not self._begun
            or self._committed
            or self._failed
            or self._moved_to_output
            or tuple(self._recorded) != (RunStageV1.RESOURCE_PREFLIGHT, RunStageV1.QWEN_LIVE_SMOKE)
            or self._cleanup_file_sha256 is not None
        ):
            raise RuntimeError("cleanup proof is outside the smoke terminal boundary")
        evidence = self._canonical_object(evidence_preimage, "cleanup")
        evidence_sha256 = hashlib.sha256(evidence_preimage).hexdigest()
        payload = _canonical_bytes(
            {
                **self._binding,
                "resource_cleanup_evidence": evidence,
                "resource_cleanup_evidence_sha256": evidence_sha256,
                "resource_cleanup_status": "SUCCEEDED",
            }
        )
        self._cleanup_file_sha256 = self._write_once("03-resource-cleanup.json", payload)
        self._cleanup_evidence_sha256 = evidence_sha256
        return evidence_sha256

    def commit(self, result: R24SmokeSequenceResultV1) -> None:
        if (
            not self._begun
            or self._committed
            or self._failed
            or self._moved_to_output
            or tuple(self._recorded) != tuple(self._STAGE_FILES)
            or self._cleanup_evidence_sha256 != result.resource_cleanup_evidence_sha256
            or self._cleanup_file_sha256 is None
            or result.status is not SequenceStatusV1.COMPLETE
            or not result.successful_output_committed
            or self._output_root.exists()
            or self._output_root.is_symlink()
        ):
            raise RuntimeError("smoke output transaction is incomplete")
        terminal_payload = _canonical_bytes(
            {
                **self._binding,
                "cleanup_file_sha256": self._cleanup_file_sha256,
                "result": r24_smoke_sequence_result_projection(result),
                "result_sha256": r24_smoke_sequence_result_sha256(result),
                "stage_file_sha256s": {
                    stage.value: self._stage_file_sha256s[stage] for stage in self._STAGE_FILES
                },
                "status": "COMPLETE",
            }
        )
        try:
            self._fsync_directory(self._staging)
            os.replace(self._staging, self._output_root)
            self._moved_to_output = True
            self._fsync_directory(self._parent)
            self._write_atomic_terminal("terminal.json", terminal_payload)
            self._fsync_directory(self._parent)
        except Exception:
            if self._moved_to_output:
                self._revoke_success_terminal_after_commit_error(terminal_payload)
            raise
        self._committed = True

    def fail(
        self,
        *,
        result: R24SmokeSequenceResultV1,
        stage_failure_evidence_preimage: bytes | None,
        resource_cleanup_evidence_preimage: bytes,
    ) -> None:
        if self._committed or self._failed or result.status is not SequenceStatusV1.FAILED:
            raise RuntimeError("smoke output transaction is already terminal")
        if not self._begun:
            self.begin()
        cleanup = self._canonical_object(resource_cleanup_evidence_preimage, "cleanup")
        cleanup_sha256 = hashlib.sha256(resource_cleanup_evidence_preimage).hexdigest()
        if cleanup_sha256 != result.resource_cleanup_evidence_sha256:
            raise RuntimeError("cleanup evidence differs from failed result")
        failure_evidence: dict[str, object] | None = None
        failure_evidence_sha256: str | None = None
        if stage_failure_evidence_preimage is not None:
            failure_evidence = self._canonical_object(stage_failure_evidence_preimage, "failure")
            failure_evidence_sha256 = hashlib.sha256(stage_failure_evidence_preimage).hexdigest()
        payload = _canonical_bytes(
            {
                **self._binding,
                "resource_cleanup_evidence": cleanup,
                "resource_cleanup_evidence_sha256": cleanup_sha256,
                "result": r24_smoke_sequence_result_projection(result),
                "result_sha256": r24_smoke_sequence_result_sha256(result),
                "stage_failure_evidence": failure_evidence,
                "stage_failure_evidence_sha256": failure_evidence_sha256,
                "stage_file_sha256s": {
                    stage.value: sha256 for stage, sha256 in self._stage_file_sha256s.items()
                },
                "status": "FAILED",
            }
        )
        self._pending_failure_payload = payload
        self._assert_failure_marker_is_unambiguous()
        self._write_atomic_terminal("failure.json", payload)
        if not self._moved_to_output:
            self._fsync_directory(self._staging)
            os.replace(self._staging, self._output_root)
            self._moved_to_output = True
            self._fsync_directory(self._parent)
        else:
            self._fsync_directory(self._output_root)
            self._fsync_directory(self._parent)
        self._failed = True

    def preserve_failure_recovery(self, failure_code: str) -> None:
        root = self._output_root if self._output_root.is_dir() else self._staging
        if not root.is_dir() or root.is_symlink():
            raise RuntimeError("smoke failure recovery directory is unavailable")
        if self._pending_failure_payload is None:
            raise RuntimeError("smoke failure recovery has no complete failure envelope")
        failure_envelope = self._canonical_object(
            self._pending_failure_payload,
            "pending failure",
        )
        failure_temporary = root / (
            ".failure.json."
            f"{hashlib.sha256(self._pending_failure_payload).hexdigest()[:16]}.partial"
        )
        temporary_metadata = self._lstat_if_present(failure_temporary)
        if temporary_metadata is not None:
            if not stat.S_ISREG(temporary_metadata.st_mode):
                raise RuntimeError("smoke failure partial marker changed type")
            failure_temporary.unlink()
            self._fsync_directory(root)
        payload = _canonical_bytes(
            {
                **self._binding,
                "failure_envelope": failure_envelope,
                "failure_envelope_sha256": hashlib.sha256(
                    self._pending_failure_payload
                ).hexdigest(),
                "failure_code": failure_code,
                "failure_marker_observed_before_recovery": self._lstat_if_present(
                    root / "failure.json"
                )
                is not None,
                "status": (
                    "SUCCESS_TERMINAL_REVOCATION_UNCONFIRMED"
                    if self._success_terminal_revocation_unconfirmed
                    else "FAILURE_PUBLICATION_INCOMPLETE"
                ),
                "terminal_marker_observed_before_recovery": self._lstat_if_present(
                    root / "terminal.json"
                )
                is not None,
            }
        )
        marker = root / "recovery.json"
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or marker.read_bytes() != payload:
                raise RuntimeError("smoke recovery marker differs")
        else:
            self._write_atomic_terminal("recovery.json", payload)
        self._fsync_directory(root)
        failure_marker = root / "failure.json"
        failure_metadata = self._lstat_if_present(failure_marker)
        if (
            failure_metadata is not None
            and stat.S_ISREG(failure_metadata.st_mode)
            and self._read_exact_regular_file(
                failure_marker,
                maximum_bytes=64 * 1024 * 1024,
            )
            == self._pending_failure_payload
        ):
            failure_marker.unlink()
            self._fsync_directory(root)
        if not self._moved_to_output:
            if self._output_root.exists() or self._output_root.is_symlink():
                raise RuntimeError("smoke recovery output root is not fresh")
            os.replace(self._staging, self._output_root)
            self._moved_to_output = True
            root = self._output_root
            self._fsync_directory(root)
        self._fsync_directory(self._parent)

    def rollback(self) -> None:
        if self._committed or self._failed or self._moved_to_output:
            return
        if self._staging.parent != self._parent:
            raise RuntimeError("smoke output staging escaped its bound parent")
        if self._staging.is_symlink():
            self._staging.unlink()
        elif self._staging.exists():
            if not self._staging.is_dir():
                raise RuntimeError("smoke output staging changed type")
            shutil.rmtree(self._staging)
        self._fsync_directory(self._parent)
        self._begun = False
        self._recorded.clear()
        self._stage_file_sha256s.clear()
        self._cleanup_evidence_sha256 = None
        self._cleanup_file_sha256 = None
        self._pending_failure_payload = None
        self._success_terminal_revocation_unconfirmed = False


class _AdapterBundleV1:
    """Identity-sealed bundle; only this module constructs instances."""

    __slots__ = (
        "pilot",
        "pilot_switch_authority",
        "pilot_switch_authority_sha256",
        "production",
        "resources",
        "secret_leases",
        "smoke",
    )

    def __init__(
        self,
        *,
        seal: object,
        resources: ResourceLifecycleAdapterPortV1,
        smoke: LiveSmokeAdapterPortV1,
        pilot: PilotAdapterPortV1,
        secret_leases: SecureSecretLeaseProviderPortV1,
        pilot_switch_authority: object | None,
        pilot_switch_authority_sha256: str | None,
        production: bool,
    ) -> None:
        if seal is not _MODULE_SEAL:
            raise ValueError("adapter bundle is not module-owned")
        self.resources = resources
        self.smoke = smoke
        self.pilot = pilot
        self.secret_leases = secret_leases
        self.pilot_switch_authority = pilot_switch_authority
        self.pilot_switch_authority_sha256 = pilot_switch_authority_sha256
        self.production = production


class _R24SmokeAdapterBundleV1:
    """Identity-sealed smoke-only bundle; it intentionally has no pilot port."""

    __slots__ = ("production", "resources", "secret_leases", "smoke")

    def __init__(
        self,
        *,
        seal: object,
        resources: ResourceLifecycleAdapterPortV1,
        smoke: LiveSmokeAdapterPortV1,
        secret_leases: SecureSecretLeaseProviderPortV1,
        production: bool,
    ) -> None:
        if seal is not _MODULE_SEAL:
            raise ValueError("smoke adapter bundle is not module-owned")
        self.resources = resources
        self.smoke = smoke
        self.secret_leases = secret_leases
        self.production = production


_MODULE_SEAL: Final[object] = object()
_PRODUCTION_EXECUTOR_SEAL: Final[object] = object()


class _StageFailure(RuntimeError):
    def __init__(self, code: str, evidence_preimage: bytes | None = None) -> None:
        self.code = code
        self.evidence_preimage = evidence_preimage
        super().__init__(code)


def _expected_units(stage: RunStageV1, manifest: R24R25RunAuthorityManifestV1) -> tuple[str, ...]:
    if stage is RunStageV1.RESOURCE_PREFLIGHT:
        return ("resources",)
    if stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}:
        host = PilotHostV1.QWEN3_VL if stage is RunStageV1.QWEN_LIVE_SMOKE else PilotHostV1.MAI_UI
        plan = next(plan for plan in manifest.smoke_plans if plan.host is host)
        return tuple(f"{host.value}:{case.mode.value}" for case in plan.cases)
    return tuple(f"pilot-cell-{index:03d}" for index, _ in enumerate(manifest.pilot.cells))


class _SequenceExecutorCoreV1:
    def __init__(
        self,
        manifest: R24R25RunAuthorityManifestV1,
        *,
        confirmed_manifest_sha256: str,
        preflight_report_sha256: str,
        factory_binding_sha256: str,
        confirmed_runtime_config_sha256: str,
        resource_cleanup_upper_bound_seconds: int,
        resource_cleanup_upper_bound_preimage: bytes,
        resource_cleanup_upper_bound_sha256: str,
        repository_root: Path,
        adapters: _AdapterBundleV1,
    ) -> None:
        if type(manifest) is not R24R25RunAuthorityManifestV1:
            raise ValueError("manifest must use the exact authority type")
        trusted_manifest = parse_authority_manifest(authority_manifest_projection(manifest))
        if trusted_manifest.schema_version != R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V2:
            raise ValueError("full executor requires exact v2 authority")
        assert trusted_manifest.resource_topology is not None
        assert trusted_manifest.runtime_config_sha256 is not None
        assert trusted_manifest.pricing_sha256 is not None
        assert trusted_manifest.sentinel_config_sha256 is not None
        assert trusted_manifest.max_resource_cleanup_wall_time_seconds is not None
        assert trusted_manifest.resource_cleanup_upper_bound_sha256 is not None
        assert trusted_manifest.max_model_switches is not None
        assert trusted_manifest.max_model_switch_wall_time_seconds is not None
        assert trusted_manifest.max_total_model_switch_wall_time_seconds is not None
        assert trusted_manifest.max_post_run_integrity_wall_time_seconds is not None
        manifest_hash = authority_manifest_sha256(trusted_manifest)
        if confirmed_manifest_sha256 != manifest_hash:
            raise ValueError("confirmed manifest SHA-256 differs")
        for value, name in (
            (preflight_report_sha256, "preflight_report_sha256"),
            (factory_binding_sha256, "factory_binding_sha256"),
            (confirmed_runtime_config_sha256, "confirmed_runtime_config_sha256"),
            (
                resource_cleanup_upper_bound_sha256,
                "resource_cleanup_upper_bound_sha256",
            ),
        ):
            if not _is_lower_hex(value, 64):
                raise ValueError(f"{name} differs")
        if (
            confirmed_runtime_config_sha256 != trusted_manifest.runtime_config_sha256
            or resource_cleanup_upper_bound_seconds
            != trusted_manifest.max_resource_cleanup_wall_time_seconds
            or resource_cleanup_upper_bound_sha256
            != trusted_manifest.resource_cleanup_upper_bound_sha256
        ):
            raise ValueError("executor resource authority differs")
        if type(adapters) is not _AdapterBundleV1:
            raise ValueError("executor adapters are not module-owned")
        self._manifest_sha256 = manifest_hash
        self._manifest = trusted_manifest
        self._run_id = trusted_manifest.run_id
        self._source_commit = trusted_manifest.source_commit
        self._preflight_report_sha256 = preflight_report_sha256
        self._factory_binding_sha256 = factory_binding_sha256
        self._runtime_config_sha256 = confirmed_runtime_config_sha256
        self._resource_topology = trusted_manifest.resource_topology
        self._max_resource_cleanup_wall_time_seconds = (
            trusted_manifest.max_resource_cleanup_wall_time_seconds
        )
        self._max_model_switches = trusted_manifest.max_model_switches
        self._max_model_switch_wall_time_seconds = (
            trusted_manifest.max_model_switch_wall_time_seconds
        )
        self._max_post_run_integrity_wall_time_seconds = (
            trusted_manifest.max_post_run_integrity_wall_time_seconds
        )
        expires = datetime.strptime(
            trusted_manifest.authorization.expires_at_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
        current = datetime.now(UTC)
        remaining_authority_ns = int((expires - current).total_seconds() * 1_000_000_000)
        maximum_sequence_ns = trusted_manifest.max_sequence_wall_time_seconds * 1_000_000_000
        if remaining_authority_ns < maximum_sequence_ns:
            raise ValueError("owner authority remaining window is shorter than sequence bound")
        started_ns = time.monotonic_ns()
        self._authority_deadline_monotonic_ns = started_ns + maximum_sequence_ns
        integrity_reserve_seconds = trusted_manifest.max_post_run_integrity_wall_time_seconds
        if type(integrity_reserve_seconds) is not int or integrity_reserve_seconds <= 0:
            raise ValueError("post-run integrity reserve is absent")
        self._post_run_integrity_deadline_monotonic_ns = self._authority_deadline_monotonic_ns
        self._cleanup_deadline_monotonic_ns = (
            self._post_run_integrity_deadline_monotonic_ns
            - integrity_reserve_seconds * 1_000_000_000
        )
        self._work_deadline_monotonic_ns = (
            self._cleanup_deadline_monotonic_ns
            - trusted_manifest.max_resource_cleanup_wall_time_seconds * 1_000_000_000
        )
        if self._work_deadline_monotonic_ns <= started_ns:
            raise ValueError("cleanup and post-run integrity reserves are unavailable")
        self._adapters = adapters
        self._output = AtomicExternalOutputTransactionV1(
            output_root=Path(trusted_manifest.output_root),
            repository_root=repository_root,
            run_id=trusted_manifest.run_id,
            source_commit=trusted_manifest.source_commit,
            manifest_sha256=manifest_hash,
            pricing_sha256=trusted_manifest.pricing_sha256,
            sentinel_config_sha256=trusted_manifest.sentinel_config_sha256,
            runtime_config_sha256=confirmed_runtime_config_sha256,
            preflight_report_sha256=preflight_report_sha256,
            factory_binding_sha256=factory_binding_sha256,
            resource_topology=trusted_manifest.resource_topology,
            resource_cleanup_upper_bound_seconds=resource_cleanup_upper_bound_seconds,
            resource_cleanup_upper_bound_preimage=resource_cleanup_upper_bound_preimage,
            resource_cleanup_upper_bound_sha256=resource_cleanup_upper_bound_sha256,
            max_model_switches=trusted_manifest.max_model_switches,
            max_model_switch_wall_time_seconds=(
                trusted_manifest.max_model_switch_wall_time_seconds
            ),
            max_total_model_switch_wall_time_seconds=(
                trusted_manifest.max_total_model_switch_wall_time_seconds
            ),
            production_evidence_required=adapters.production,
            pilot_switch_authority_sha256=adapters.pilot_switch_authority_sha256,
        )
        self._state = ExecutorStateV1.READY
        self._next_stage_index = 0
        self._receipts: list[StageExecutionReceiptV1] = []
        self._secret_leases_acquired = 0
        self._secret_leases_closed = 0
        self._cleanup_attempted = False
        self._cleanup_succeeded = False
        self._resource_cleanup_failure_code: str | None = None
        self._resource_cleanup_failure_evidence: bytes | None = None
        self._resource_cleanup_evidence: bytes | None = None
        self._cleanup_wall_time_ms = 0
        self._handoff_evidence_sha256: str | None = None
        self._sequence_started_ns: int | None = None
        self._sequence_wall_time_ms = 0
        self._lock = threading.Lock()

    def _context(
        self,
        manifest: R24R25RunAuthorityManifestV1,
        *,
        cleanup: bool = False,
    ) -> StageAdapterContextV1:
        consumed_wall_time_ms = sum(receipt.wall_time_ms for receipt in self._receipts)
        if cleanup:
            deadline_ns = self._cleanup_deadline_monotonic_ns
            actual_remaining_wall_time_ms = max(0, (deadline_ns - time.monotonic_ns()) // 1_000_000)
            remaining_wall_time_ms = min(
                self._max_resource_cleanup_wall_time_seconds * 1000,
                actual_remaining_wall_time_ms,
            )
        else:
            declared_remaining_wall_time_ms = (
                manifest.max_sequence_wall_time_seconds
                - self._max_resource_cleanup_wall_time_seconds
                - self._max_post_run_integrity_wall_time_seconds
            ) * 1000 - consumed_wall_time_ms
            deadline_ns = self._work_deadline_monotonic_ns
            actual_remaining_wall_time_ms = max(0, (deadline_ns - time.monotonic_ns()) // 1_000_000)
            remaining_wall_time_ms = min(
                declared_remaining_wall_time_ms, actual_remaining_wall_time_ms
            )
        return StageAdapterContextV1(
            manifest_sha256=self._manifest_sha256,
            sequence_execution_scope="R24_R25_FULL",
            sequence_scope_authority_sha256=self._manifest_sha256,
            run_id=self._run_id,
            source_commit=self._source_commit,
            remaining_actor_calls=manifest.max_sequence_actor_calls
            - sum(receipt.actor_calls for receipt in self._receipts),
            remaining_openai_calls=manifest.max_sequence_openai_calls
            - sum(receipt.openai_calls for receipt in self._receipts),
            remaining_cost_usd_micros=manifest.max_sequence_cost_usd_micros
            - sum(receipt.cost_usd_micros for receipt in self._receipts),
            remaining_wall_time_ms=max(0, remaining_wall_time_ms),
            authority_deadline_monotonic_ns=deadline_ns,
        )

    def _cleanup(self, context: StageAdapterContextV1) -> bool:
        del context
        if self._cleanup_succeeded:
            return True
        self._cleanup_attempted = True
        cleanup_context = self._context(self._manifest, cleanup=True)
        started_ns = time.monotonic_ns()
        try:
            self._adapters.resources.cleanup(cleanup_context)
        except Exception as exc:
            self._cleanup_wall_time_ms = (time.monotonic_ns() - started_ns + 999_999) // 1_000_000
            self._cleanup_succeeded = False
            code = getattr(exc, "code", "RESOURCE_CLEANUP_FAILED")
            self._resource_cleanup_failure_code = (
                code if type(code) is str else "RESOURCE_CLEANUP_FAILED"
            )
            try:
                self._resource_cleanup_failure_evidence = (
                    self._adapters.resources.failure_evidence_preimage(
                        RunStageV1.RESOURCE_PREFLIGHT
                    )
                )
            except Exception:
                self._resource_cleanup_failure_evidence = None
            return False
        elapsed_ns = time.monotonic_ns() - started_ns
        self._cleanup_wall_time_ms = (elapsed_ns + 999_999) // 1_000_000
        try:
            self._resource_cleanup_evidence = (
                self._adapters.resources.cleanup_success_evidence_preimage()
            )
        except Exception:
            self._resource_cleanup_evidence = None
        if (
            elapsed_ns > self._max_resource_cleanup_wall_time_seconds * 1_000_000_000
            or time.monotonic_ns() > self._cleanup_deadline_monotonic_ns
            or self._resource_cleanup_evidence is None
        ):
            self._cleanup_succeeded = False
            self._resource_cleanup_failure_code = "RESOURCE_CLEANUP_PROOF_OR_BOUND_FAILED"
            if self._resource_cleanup_evidence is None:
                try:
                    self._resource_cleanup_failure_evidence = (
                        self._adapters.resources.failure_evidence_preimage(
                            RunStageV1.RESOURCE_PREFLIGHT
                        )
                    )
                except Exception:
                    self._resource_cleanup_failure_evidence = None
            else:
                self._resource_cleanup_failure_evidence = self._resource_cleanup_evidence
            return False
        self._cleanup_succeeded = True
        self._resource_cleanup_failure_code = None
        self._resource_cleanup_failure_evidence = None
        return True

    def _abort(
        self,
        code: str,
        context: StageAdapterContextV1,
        *,
        failed_stage: RunStageV1,
        failure_evidence_preimage: bytes | None,
    ) -> None:
        cleanup_ok = self._cleanup(context)
        if self._sequence_started_ns is not None:
            self._sequence_wall_time_ms = (
                time.monotonic_ns() - self._sequence_started_ns + 999_999
            ) // 1_000_000
        failure_publish_ok = True
        try:
            self._output.fail(
                failed_stage=failed_stage,
                failure_code=code,
                stage_failure_evidence_preimage=failure_evidence_preimage,
                resource_cleanup_evidence_preimage=(
                    self._resource_cleanup_evidence
                    if cleanup_ok
                    else self._resource_cleanup_failure_evidence
                ),
                resource_cleanup_failure_code=(
                    None if cleanup_ok else self._resource_cleanup_failure_code
                ),
                resource_cleanup_status=(
                    "SUCCEEDED"
                    if cleanup_ok
                    else "OWNED_RESOURCES_CLEANED_TENANT_DRIFT"
                    if self._resource_cleanup_failure_code == "GPU_SHARED_TENANT_DRIFT"
                    else "RETRY_REQUIRED"
                ),
                cleanup_wall_time_ms=self._cleanup_wall_time_ms,
                sequence_wall_time_ms=self._sequence_wall_time_ms,
            )
        except Exception:
            failure_publish_ok = False
            try:
                self._output.preserve_failure_recovery(
                    failed_stage=failed_stage,
                    failure_code=code,
                )
            except Exception:
                pass
        self._state = ExecutorStateV1.FAILED
        if not cleanup_ok:
            raise LiveRunContractError(
                code, "executor retained the first failure; cleanup also failed"
            ) from None
        if not failure_publish_ok:
            raise LiveRunContractError(
                code,
                "executor retained the first failure; failure publication also failed",
            ) from None
        raise LiveRunContractError(code, "executor stopped fail-closed") from None

    def _adapter_result(
        self,
        stage: RunStageV1,
        manifest: R24R25RunAuthorityManifestV1,
        context: StageAdapterContextV1,
    ) -> AdapterStageResultV1:
        if stage is RunStageV1.RESOURCE_PREFLIGHT:
            try:
                if self._adapters.production:
                    return self._adapters.resources.prepare(  # type: ignore[call-arg]
                        manifest.actor_resources,
                        context,
                        pilot_switch_authority=self._adapters.pilot_switch_authority,
                    )
                return self._adapters.resources.prepare(manifest.actor_resources, context)
            except Exception as exc:
                primary_code = _adapter_error_code(exc, "RESOURCE_ADAPTER_FAILED")
                try:
                    evidence = self._adapters.resources.failure_evidence_preimage(stage)
                except Exception as evidence_exc:
                    evidence = _canonical_bytes(
                        {
                            "domain": "r24-r25-full-stage-failure-evidence",
                            "manifest_sha256": self._manifest_sha256,
                            "primary_failure_code": primary_code,
                            "secondary_failure_codes": [
                                _adapter_error_code(
                                    evidence_exc,
                                    "STAGE_FAILURE_EVIDENCE_RETRIEVAL_FAILED",
                                )
                            ],
                            "stage": stage.value,
                        }
                    )
                raise _StageFailure(primary_code, evidence) from None
        handoff_result: AdapterStageResultV1 | None = None
        if stage is RunStageV1.MAI_LIVE_SMOKE:
            try:
                if (
                    self._adapters.production
                    and self._resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
                ):
                    switch_deadline_ns = min(
                        context.authority_deadline_monotonic_ns,
                        time.monotonic_ns()
                        + self._max_model_switch_wall_time_seconds * 1_000_000_000,
                    )
                    handoff_result = self._adapters.resources.handoff_to_mai(  # type: ignore[call-arg]
                        context,
                        switch_deadline_monotonic_ns=switch_deadline_ns,
                    )
                else:
                    handoff_result = self._adapters.resources.handoff_to_mai(context)
            except Exception as exc:
                primary_code = _adapter_error_code(exc, "QWEN_TO_MAI_HANDOFF_FAILED")
                try:
                    evidence = self._adapters.resources.failure_evidence_preimage(stage)
                except Exception as evidence_exc:
                    evidence = _canonical_bytes(
                        {
                            "domain": "r24-r25-full-stage-failure-evidence",
                            "manifest_sha256": self._manifest_sha256,
                            "primary_failure_code": primary_code,
                            "secondary_failure_codes": [
                                _adapter_error_code(
                                    evidence_exc,
                                    "STAGE_FAILURE_EVIDENCE_RETRIEVAL_FAILED",
                                )
                            ],
                            "stage": stage.value,
                        }
                    )
                raise _StageFailure(primary_code, evidence) from None
            expected_handoff_units = (
                ("resource-handoff:QWEN3_VL:MAI_UI",)
                if self._resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
                else ("resource-handoff:not-required-concurrent",)
            )
            if (
                type(handoff_result) is not AdapterStageResultV1
                or handoff_result.stage is not RunStageV1.MAI_LIVE_SMOKE
                or handoff_result.manifest_sha256 != self._manifest_sha256
                or handoff_result.actor_calls != 0
                or handoff_result.openai_calls != 0
                or handoff_result.actor_actions != 0
                or handoff_result.cost_usd_micros != 0
                or handoff_result.completed_units != expected_handoff_units
                or handoff_result.provider_final_request_proven
            ):
                raise _StageFailure("INVALID_HANDOFF_RESULT")
            self._handoff_evidence_sha256 = (
                handoff_result.evidence_sha256
                if self._resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
                else None
            )
        lease: CaseAuthorityBrokerV1
        try:
            lease = self._adapters.secret_leases.acquire(
                manifest.secret,
                manifest_sha256=self._manifest_sha256,
            )
        except Exception:
            # Stable v1 code retained for CPU/external receipt compatibility;
            # the acquired object is now a no-secret case broker.
            raise _StageFailure("SECRET_LEASE_UNAVAILABLE") from None
        if not isinstance(lease, CaseAuthorityBrokerV1):
            raise _StageFailure("INVALID_CASE_AUTHORITY_BROKER")
        if (
            lease.manifest_sha256 != self._manifest_sha256
            or lease.environment_key != manifest.secret.environment_key
        ):
            try:
                lease.close()
            except Exception:
                pass
            raise _StageFailure("INVALID_CASE_AUTHORITY_BROKER")
        self._secret_leases_acquired += 1
        adapter_failure_code: str | None = None
        failure_evidence: bytes | None = None
        returned_result_evidence: bytes | None = None
        secondary_failure_codes: list[str] = []
        result: AdapterStageResultV1 | None = None
        try:
            if stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}:
                host = (
                    PilotHostV1.QWEN3_VL
                    if stage is RunStageV1.QWEN_LIVE_SMOKE
                    else PilotHostV1.MAI_UI
                )
                plan = next(plan for plan in manifest.smoke_plans if plan.host is host)
                resource = next(
                    resource for resource in manifest.actor_resources if resource.host is host
                )
                result = self._adapters.smoke.run_host(
                    host,
                    plan,
                    resource,
                    manifest.openai_stages,
                    context,
                    lease,
                )
            else:
                result = self._adapters.pilot.run_pilot(
                    manifest.pilot,
                    manifest.actor_resources,
                    manifest.openai_stages,
                    context,
                    lease,
                )
        except Exception as exc:
            adapter_failure_code = _adapter_error_code(exc, "STAGE_ADAPTER_FAILED")
            failed_adapter: object = (
                self._adapters.smoke
                if stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}
                else self._adapters.pilot
            )
            try:
                candidate_failure = failed_adapter.failure_evidence_preimage(stage)  # type: ignore[attr-defined]
                if candidate_failure is not None:
                    if type(candidate_failure) is not bytes:
                        raise TypeError("failure evidence type differs")
                    decoded_failure = json.loads(candidate_failure)
                    if _canonical_bytes(decoded_failure) != candidate_failure:
                        raise ValueError("failure evidence is not canonical")
                    failure_evidence = candidate_failure
            except Exception as evidence_exc:
                secondary_failure_codes.append(
                    _adapter_error_code(evidence_exc, "STAGE_FAILURE_EVIDENCE_RETRIEVAL_FAILED")
                )
                failure_evidence = None
        if adapter_failure_code is None:
            try:
                if type(result) is not AdapterStageResultV1 or result.stage is not stage:
                    raise _StageFailure("INVALID_ADAPTER_RESULT")
                # Preserve a returned durable stage result before performing
                # executor-side admission.  A later census/budget rejection or
                # broker-close fault must not discard completed unit journals.
                returned_result_evidence = result.evidence_preimage
                self._validate_stage_receipt(
                    StageExecutionReceiptV1(
                        stage=stage,
                        manifest_sha256=result.manifest_sha256,
                        passed=True,
                        evidence_sha256=result.evidence_sha256,
                        actor_calls=result.actor_calls,
                        openai_calls=result.openai_calls,
                        actor_actions=result.actor_actions,
                        cost_usd_micros=result.cost_usd_micros,
                        wall_time_ms=0,
                        completed_units=result.completed_units,
                        provider_final_request_proven=(result.provider_final_request_proven),
                    ),
                    manifest,
                )
            except _StageFailure as exc:
                adapter_failure_code = exc.code
                failure_evidence = returned_result_evidence
        broker_close_code: str | None = None
        try:
            lease.close()
            self._secret_leases_closed += 1
        except Exception as exc:
            broker_close_code = _adapter_error_code(exc, "CASE_AUTHORITY_BROKER_CLOSE_FAILED")
            if adapter_failure_code is not None:
                secondary_failure_codes.append(broker_close_code)
        if adapter_failure_code is not None:
            if handoff_result is not None:
                handoff_failure_value = (
                    None if failure_evidence is None else cast(object, json.loads(failure_evidence))
                )
                failure_evidence = _canonical_bytes(
                    {
                        "domain": "r24-r25-full-mai-stage-failure-evidence",
                        "handoff_evidence": json.loads(handoff_result.evidence_preimage),
                        "handoff_evidence_sha256": handoff_result.evidence_sha256,
                        "manifest_sha256": self._manifest_sha256,
                        "smoke_failure_evidence": handoff_failure_value,
                        "smoke_failure_evidence_sha256": (
                            None
                            if failure_evidence is None
                            else hashlib.sha256(failure_evidence).hexdigest()
                        ),
                        "primary_failure_code": adapter_failure_code,
                        "secondary_failure_codes": secondary_failure_codes,
                    }
                )
            elif returned_result_evidence is not None or secondary_failure_codes:
                failure_value: object = None
                failure_value_sha256: str | None = None
                if failure_evidence is not None:
                    failure_value = json.loads(failure_evidence)
                    failure_value_sha256 = hashlib.sha256(failure_evidence).hexdigest()
                failure_evidence = _canonical_bytes(
                    {
                        "adapter_failure_evidence": failure_value,
                        "adapter_failure_evidence_sha256": failure_value_sha256,
                        "domain": "r24-r25-full-stage-failure-evidence",
                        "manifest_sha256": self._manifest_sha256,
                        "primary_failure_code": adapter_failure_code,
                        "secondary_failure_codes": secondary_failure_codes,
                        "stage": stage.value,
                    }
                )
            raise _StageFailure(adapter_failure_code, failure_evidence)
        if broker_close_code is not None:
            assert type(result) is AdapterStageResultV1
            if handoff_result is not None:
                close_failure_evidence = _canonical_bytes(
                    {
                        "domain": "r24-r25-full-mai-stage-failure-evidence",
                        "handoff_evidence": json.loads(handoff_result.evidence_preimage),
                        "handoff_evidence_sha256": handoff_result.evidence_sha256,
                        "manifest_sha256": self._manifest_sha256,
                        "primary_failure_code": broker_close_code,
                        "secondary_failure_codes": [],
                        "smoke_completed_evidence": json.loads(result.evidence_preimage),
                        "smoke_completed_evidence_sha256": result.evidence_sha256,
                    }
                )
            else:
                close_failure_evidence = _canonical_bytes(
                    {
                        "completed_adapter_evidence": json.loads(result.evidence_preimage),
                        "completed_adapter_evidence_sha256": result.evidence_sha256,
                        "domain": "r24-r25-full-stage-failure-evidence",
                        "manifest_sha256": self._manifest_sha256,
                        "primary_failure_code": broker_close_code,
                        "secondary_failure_codes": [],
                        "stage": stage.value,
                    }
                )
            raise _StageFailure(broker_close_code, close_failure_evidence) from None
        assert type(result) is AdapterStageResultV1
        if handoff_result is not None:
            smoke_evidence = json.loads(result.evidence_preimage)
            combined = _canonical_bytes(
                {
                    "domain": "r24-r25-full-mai-stage-evidence",
                    "handoff_evidence": json.loads(handoff_result.evidence_preimage),
                    "handoff_evidence_sha256": handoff_result.evidence_sha256,
                    "manifest_sha256": self._manifest_sha256,
                    "smoke_evidence": smoke_evidence,
                    "smoke_evidence_sha256": result.evidence_sha256,
                }
            )
            result = AdapterStageResultV1(
                stage=result.stage,
                manifest_sha256=result.manifest_sha256,
                evidence_sha256=hashlib.sha256(combined).hexdigest(),
                evidence_preimage=combined,
                actor_calls=result.actor_calls,
                openai_calls=result.openai_calls,
                actor_actions=result.actor_actions,
                cost_usd_micros=result.cost_usd_micros,
                completed_units=result.completed_units,
                provider_final_request_proven=result.provider_final_request_proven,
            )
        return result

    def _validate_stage_receipt(
        self,
        receipt: StageExecutionReceiptV1,
        manifest: R24R25RunAuthorityManifestV1,
    ) -> None:
        if (
            receipt.manifest_sha256 != self._manifest_sha256
            or receipt.completed_units != _expected_units(receipt.stage, manifest)
        ):
            raise _StageFailure("STAGE_BINDING_MISMATCH")
        if receipt.stage is RunStageV1.RESOURCE_PREFLIGHT:
            valid = (
                receipt.actor_calls == 0
                and receipt.openai_calls == 0
                and receipt.actor_actions == 0
                and receipt.cost_usd_micros == 0
                and receipt.wall_time_ms <= manifest.max_resource_preflight_wall_time_seconds * 1000
                and not receipt.provider_final_request_proven
            )
        elif receipt.stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}:
            host = (
                PilotHostV1.QWEN3_VL
                if receipt.stage is RunStageV1.QWEN_LIVE_SMOKE
                else PilotHostV1.MAI_UI
            )
            plan = next(plan for plan in manifest.smoke_plans if plan.host is host)
            handoff_seconds = (
                self._max_model_switch_wall_time_seconds
                if (
                    host is PilotHostV1.MAI_UI
                    and self._resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
                )
                else 0
            )
            minimum_openai_calls = sum(
                0 if case.mode is SmokeModeV1.OFF else 2 for case in plan.cases
            )
            maximum_openai_calls = sum(case.max_openai_calls for case in plan.cases)
            valid = (
                receipt.actor_calls == sum(case.max_actor_calls for case in plan.cases)
                and minimum_openai_calls <= receipt.openai_calls <= maximum_openai_calls
                and receipt.actor_actions == 0
                and receipt.cost_usd_micros <= sum(case.max_cost_usd_micros for case in plan.cases)
                and receipt.wall_time_ms
                <= (sum(case.max_wall_time_seconds for case in plan.cases) + handoff_seconds) * 1000
                and receipt.provider_final_request_proven
            )
        else:
            valid = (
                len(manifest.pilot.cells)
                <= receipt.actor_calls
                <= manifest.pilot.max_total_actor_calls
                and receipt.openai_calls <= manifest.pilot.max_total_openai_calls
                and receipt.actor_actions <= receipt.actor_calls
                and receipt.cost_usd_micros <= manifest.pilot.max_total_cost_usd_micros
                and receipt.wall_time_ms
                <= (
                    manifest.pilot.max_total_wall_time_seconds
                    + (
                        max(0, self._max_model_switches - 1)
                        * self._max_model_switch_wall_time_seconds
                    )
                )
                * 1000
                and receipt.provider_final_request_proven
            )
        if not valid:
            raise _StageFailure("STAGE_BUDGET_OR_CENSUS_MISMATCH")
        projected = self._receipts + [receipt]
        if (
            sum(item.actor_calls for item in projected) > manifest.max_sequence_actor_calls
            or sum(item.openai_calls for item in projected) > manifest.max_sequence_openai_calls
            or sum(item.cost_usd_micros for item in projected)
            > manifest.max_sequence_cost_usd_micros
            or sum(item.wall_time_ms for item in projected)
            > manifest.max_sequence_wall_time_seconds * 1000
        ):
            raise _StageFailure("SEQUENCE_BUDGET_EXCEEDED")

    def run_stage(
        self, stage: RunStageV1, manifest: R24R25RunAuthorityManifestV1
    ) -> StageExecutionReceiptV1:
        with self._lock:
            trusted_manifest = self._manifest
            current_stage_preimage: bytes | None = None
            if self._state in {ExecutorStateV1.COMPLETE, ExecutorStateV1.FAILED}:
                raise LiveRunContractError("EXECUTOR_STOPPED", "executor is terminal")
            if self._sequence_started_ns is None:
                self._sequence_started_ns = time.monotonic_ns()
            context = self._context(trusted_manifest)
            try:
                if (
                    type(stage) is not RunStageV1
                    or type(manifest) is not R24R25RunAuthorityManifestV1
                ):
                    raise _StageFailure("UNTRUSTED_EXECUTOR_INPUT")
                if authority_manifest_sha256(manifest) != self._manifest_sha256:
                    raise _StageFailure("MANIFEST_BINDING_MISMATCH")
                expected_stage = trusted_manifest.safety.stages[self._next_stage_index]
                if stage is not expected_stage:
                    raise _StageFailure("STAGE_ORDER_VIOLATION")
                if time.monotonic_ns() >= context.authority_deadline_monotonic_ns:
                    raise _StageFailure("OWNER_AUTHORITY_EXPIRED")
                required_stage_seconds = (
                    trusted_manifest.max_resource_preflight_wall_time_seconds
                    if stage is RunStageV1.RESOURCE_PREFLIGHT
                    else (
                        sum(
                            case.max_wall_time_seconds
                            for plan in trusted_manifest.smoke_plans
                            if plan.host
                            is (
                                PilotHostV1.QWEN3_VL
                                if stage is RunStageV1.QWEN_LIVE_SMOKE
                                else PilotHostV1.MAI_UI
                            )
                            for case in plan.cases
                        )
                        + (
                            self._max_model_switch_wall_time_seconds
                            if stage is RunStageV1.MAI_LIVE_SMOKE
                            and self._resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
                            else 0
                        )
                        if stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}
                        else trusted_manifest.pilot.max_total_wall_time_seconds
                        + max(0, self._max_model_switches - 1)
                        * self._max_model_switch_wall_time_seconds
                    )
                )
                if context.remaining_wall_time_ms < required_stage_seconds * 1000:
                    raise _StageFailure("STAGE_WALL_RESERVATION_UNAVAILABLE")
                started_ns = time.monotonic_ns()
                if stage is RunStageV1.RESOURCE_PREFLIGHT:
                    stage_deadline_ns = min(
                        context.authority_deadline_monotonic_ns,
                        started_ns + required_stage_seconds * 1_000_000_000,
                    )
                    stage_remaining_wall_time_ms = max(
                        0, (stage_deadline_ns - started_ns) // 1_000_000
                    )
                    context = StageAdapterContextV1(
                        manifest_sha256=context.manifest_sha256,
                        sequence_execution_scope=context.sequence_execution_scope,
                        sequence_scope_authority_sha256=(context.sequence_scope_authority_sha256),
                        run_id=context.run_id,
                        source_commit=context.source_commit,
                        remaining_actor_calls=context.remaining_actor_calls,
                        remaining_openai_calls=context.remaining_openai_calls,
                        remaining_cost_usd_micros=context.remaining_cost_usd_micros,
                        remaining_wall_time_ms=min(
                            context.remaining_wall_time_ms,
                            stage_remaining_wall_time_ms,
                        ),
                        authority_deadline_monotonic_ns=stage_deadline_ns,
                    )
                self._state = ExecutorStateV1.RUNNING
                try:
                    self._output.begin()
                except Exception:
                    raise _StageFailure("OUTPUT_TRANSACTION_FAILED") from None
                result = self._adapter_result(stage, trusted_manifest, context)
                current_stage_preimage = result.evidence_preimage
                elapsed_ms = (time.monotonic_ns() - started_ns + 999_999) // 1_000_000
                if type(result) is not AdapterStageResultV1 or result.stage is not stage:
                    raise _StageFailure("INVALID_ADAPTER_RESULT")
                receipt = StageExecutionReceiptV1(
                    stage=stage,
                    manifest_sha256=result.manifest_sha256,
                    passed=True,
                    evidence_sha256=result.evidence_sha256,
                    actor_calls=result.actor_calls,
                    openai_calls=result.openai_calls,
                    actor_actions=result.actor_actions,
                    cost_usd_micros=result.cost_usd_micros,
                    wall_time_ms=elapsed_ms,
                    completed_units=result.completed_units,
                    provider_final_request_proven=result.provider_final_request_proven,
                )
                self._validate_stage_receipt(receipt, trusted_manifest)
                try:
                    self._output.record(receipt, result.evidence_preimage)
                except Exception:
                    raise _StageFailure("OUTPUT_TRANSACTION_FAILED") from None
                if stage is RunStageV1.R25_PILOT:
                    if not self._cleanup(context):
                        raise _StageFailure(
                            self._resource_cleanup_failure_code or "EXECUTOR_CLEANUP_FAILED"
                        )
                    assert self._resource_cleanup_evidence is not None
                    try:
                        self._output.record_cleanup_success(self._resource_cleanup_evidence)
                    except Exception:
                        raise _StageFailure("CLEANUP_PUBLICATION_FAILED") from None
                    terminal_receipts = tuple((*self._receipts, receipt))
                    terminal_result = SequenceRunResultV1(
                        schema_version=R24_R25_SEQUENCE_RESULT_SCHEMA_VERSION,
                        run_id=self._run_id,
                        manifest_sha256=self._manifest_sha256,
                        status=SequenceStatusV1.COMPLETE,
                        receipts=terminal_receipts,
                        failed_stage=None,
                        failure_code=None,
                    )
                    terminal_census: dict[str, object] = {
                        "actor_actions": sum(item.actor_actions for item in terminal_receipts),
                        "actor_calls": sum(item.actor_calls for item in terminal_receipts),
                        "cleanup_wall_time_ms": self._cleanup_wall_time_ms,
                        "cost_usd_micros": sum(item.cost_usd_micros for item in terminal_receipts),
                        "openai_calls": sum(item.openai_calls for item in terminal_receipts),
                        "handoff_evidence_sha256": self._handoff_evidence_sha256,
                        "receipts": list(terminal_receipts),
                        "secret_leases_acquired": self._secret_leases_acquired,
                        "secret_leases_closed": self._secret_leases_closed,
                        "stage_wall_time_ms": sum(item.wall_time_ms for item in terminal_receipts),
                    }
                    assert self._sequence_started_ns is not None
                    self._sequence_wall_time_ms = (
                        time.monotonic_ns() - self._sequence_started_ns + 999_999
                    ) // 1_000_000
                    terminal_census["wall_time_ms"] = self._sequence_wall_time_ms
                    terminal_wall_time_ms = self._sequence_wall_time_ms
                    if (
                        terminal_wall_time_ms
                        > trusted_manifest.max_sequence_wall_time_seconds * 1000
                        or (
                            self._resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
                            and self._handoff_evidence_sha256 is None
                        )
                    ):
                        raise _StageFailure("SEQUENCE_BUDGET_OR_HANDOFF_EVIDENCE_MISMATCH")
                    try:
                        self._output.commit(
                            terminal_result,
                            census=terminal_census,
                            publication_deadline_monotonic_ns=(self._cleanup_deadline_monotonic_ns),
                        )
                    except Exception:
                        raise _StageFailure("OUTPUT_TRANSACTION_FAILED") from None
                    self._state = ExecutorStateV1.COMPLETE
                self._receipts.append(receipt)
                self._next_stage_index += 1
                return receipt
            except _StageFailure as exc:
                self._abort(
                    exc.code,
                    context,
                    failed_stage=stage,
                    failure_evidence_preimage=(
                        exc.evidence_preimage
                        if exc.evidence_preimage is not None
                        else current_stage_preimage
                    ),
                )
            except Exception:
                self._abort(
                    "EXECUTOR_INTERNAL_FAILURE",
                    context,
                    failed_stage=stage,
                    failure_evidence_preimage=current_stage_preimage,
                )
        raise AssertionError("unreachable executor state")

    @property
    def census(self) -> ExecutorCensusV1:
        with self._lock:
            return ExecutorCensusV1(
                state=self._state,
                manifest_sha256=self._manifest_sha256,
                completed_stages=tuple(receipt.stage for receipt in self._receipts),
                actor_calls=sum(receipt.actor_calls for receipt in self._receipts),
                openai_calls=sum(receipt.openai_calls for receipt in self._receipts),
                actor_actions=sum(receipt.actor_actions for receipt in self._receipts),
                cost_usd_micros=sum(receipt.cost_usd_micros for receipt in self._receipts),
                wall_time_ms=(
                    self._sequence_wall_time_ms
                    if self._sequence_wall_time_ms
                    else sum(receipt.wall_time_ms for receipt in self._receipts)
                ),
                secret_leases_acquired=self._secret_leases_acquired,
                secret_leases_closed=self._secret_leases_closed,
                cleanup_attempted=self._cleanup_attempted,
                cleanup_succeeded=self._cleanup_succeeded,
                output_committed=self._output.committed,
            )

    @property
    def sequence_started_monotonic_ns(self) -> int:
        with self._lock:
            if self._sequence_started_ns is None:
                raise LiveRunContractError(
                    "SEQUENCE_NOT_STARTED", "sequence start is not yet frozen"
                )
            return self._sequence_started_ns

    @property
    def post_run_integrity_deadline_monotonic_ns(self) -> int:
        """Absolute deadline left after execution and cleanup for the sealed gate."""

        return self._post_run_integrity_deadline_monotonic_ns

    @property
    def post_run_integrity_output_root(self) -> Path:
        return self._output.output_root

    @property
    def preflight_report_sha256(self) -> str:
        return self._preflight_report_sha256

    @property
    def factory_binding_sha256(self) -> str:
        return self._factory_binding_sha256

    @property
    def runtime_config_sha256(self) -> str:
        return self._runtime_config_sha256


class ProductionR24R25ExecutorV1(_SequenceExecutorCoreV1):
    """Production stage executor created only by the exact dependency factory."""

    def __init__(
        self,
        manifest: R24R25RunAuthorityManifestV1,
        *,
        confirmed_manifest_sha256: str,
        preflight_report_sha256: str,
        factory_binding_sha256: str,
        confirmed_runtime_config_sha256: str,
        resource_cleanup_upper_bound_seconds: int,
        resource_cleanup_upper_bound_preimage: bytes,
        resource_cleanup_upper_bound_sha256: str,
        repository_root: Path,
        module_owned_adapters: object,
        seal: object | None = None,
    ) -> None:
        if (
            seal is not _PRODUCTION_EXECUTOR_SEAL
            or type(module_owned_adapters) is not _AdapterBundleV1
            or not module_owned_adapters.production
        ):
            raise LiveRunContractError(
                "PRODUCTION_ADAPTERS_UNAVAILABLE",
                "reviewed module-owned production adapters are required",
            )
        super().__init__(
            manifest,
            confirmed_manifest_sha256=confirmed_manifest_sha256,
            preflight_report_sha256=preflight_report_sha256,
            factory_binding_sha256=factory_binding_sha256,
            confirmed_runtime_config_sha256=confirmed_runtime_config_sha256,
            resource_cleanup_upper_bound_seconds=resource_cleanup_upper_bound_seconds,
            resource_cleanup_upper_bound_preimage=resource_cleanup_upper_bound_preimage,
            resource_cleanup_upper_bound_sha256=resource_cleanup_upper_bound_sha256,
            repository_root=repository_root,
            adapters=module_owned_adapters,
        )


def _smoke_evidence_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > 64 * 1024 * 1024:
        raise _StageFailure("INVALID_ADAPTER_EVIDENCE")
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        raise _StageFailure("INVALID_ADAPTER_EVIDENCE") from None
    if type(value) is not dict or _canonical_bytes(value) != raw:
        raise _StageFailure("INVALID_ADAPTER_EVIDENCE")
    del label
    return value


def _adapter_error_code(exc: BaseException, fallback: str) -> str:
    code = getattr(exc, "code", fallback)
    return code if type(code) is str and code else fallback


class _R24SmokeExecutorCoreV1:
    """Independent executor for exactly six R2.4 smoke cases and no pilot."""

    _STAGES: Final[tuple[RunStageV1, ...]] = (
        RunStageV1.RESOURCE_PREFLIGHT,
        RunStageV1.QWEN_LIVE_SMOKE,
        RunStageV1.MAI_LIVE_SMOKE,
    )

    def __init__(
        self,
        manifest: R24SmokeRunAuthorityManifestV1,
        *,
        confirmed_manifest_sha256: str,
        preflight_report_sha256: str,
        factory_binding_sha256: str,
        confirmed_runtime_config_sha256: str,
        resource_cleanup_upper_bound_seconds: int,
        resource_cleanup_upper_bound_preimage: bytes,
        resource_cleanup_upper_bound_sha256: str,
        repository_root: Path,
        adapters: _R24SmokeAdapterBundleV1,
    ) -> None:
        if type(manifest) is not R24SmokeRunAuthorityManifestV1:
            raise ValueError("manifest must use the exact R2.4 smoke authority type")
        trusted = parse_smoke_authority_manifest(smoke_authority_manifest_projection(manifest))
        manifest_sha256 = smoke_authority_manifest_sha256(trusted)
        if confirmed_manifest_sha256 != manifest_sha256:
            raise ValueError("confirmed smoke manifest SHA-256 differs")
        for value, name in (
            (preflight_report_sha256, "preflight_report_sha256"),
            (factory_binding_sha256, "factory_binding_sha256"),
            (confirmed_runtime_config_sha256, "confirmed_runtime_config_sha256"),
        ):
            if not _is_lower_hex(value, 64):
                raise ValueError(f"{name} is invalid")
        if confirmed_runtime_config_sha256 != trusted.runtime_config_sha256:
            raise ValueError("confirmed runtime config SHA-256 differs")
        if (
            type(resource_cleanup_upper_bound_seconds) is not int
            or resource_cleanup_upper_bound_seconds < R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS
            or trusted.max_resource_cleanup_wall_time_seconds < resource_cleanup_upper_bound_seconds
            or type(resource_cleanup_upper_bound_preimage) is not bytes
            or not _is_lower_hex(resource_cleanup_upper_bound_sha256, 64)
            or hashlib.sha256(resource_cleanup_upper_bound_preimage).hexdigest()
            != resource_cleanup_upper_bound_sha256
        ):
            raise ValueError("resource cleanup upper bound binding differs")
        if (
            type(adapters) is not _R24SmokeAdapterBundleV1
            or trusted.execution_scope is not SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY
            or trusted.safety.stages != self._STAGES
            or not trusted.safety.pilot_stage_forbidden
        ):
            raise ValueError("smoke executor authority or adapters differ")
        if trusted.authorization.status is not RunAuthorizationStatusV1.OWNER_AUTHORIZED:
            raise ValueError("smoke executor requires OWNER_AUTHORIZED authority")
        expires = datetime.strptime(
            trusted.authorization.expires_at_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
        issued = datetime.strptime(
            trusted.authorization.issued_at_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
        now = datetime.now(UTC)
        if not issued <= now < expires:
            raise ValueError("smoke executor authority is not current")
        remaining_authority_ns = int((expires - now).total_seconds() * 1_000_000_000)
        self._authority_deadline_monotonic_ns = time.monotonic_ns() + max(0, remaining_authority_ns)
        self._manifest = trusted
        self._manifest_sha256 = manifest_sha256
        self._preflight_report_sha256 = preflight_report_sha256
        self._factory_binding_sha256 = factory_binding_sha256
        self._runtime_config_sha256 = confirmed_runtime_config_sha256
        self._resource_cleanup_upper_bound_seconds = resource_cleanup_upper_bound_seconds
        self._resource_cleanup_upper_bound_preimage = resource_cleanup_upper_bound_preimage
        self._resource_cleanup_upper_bound_sha256 = resource_cleanup_upper_bound_sha256
        self._adapters = adapters
        self._output = AtomicR24SmokeOutputTransactionV1(
            output_root=Path(trusted.output_root),
            repository_root=repository_root,
            run_id=trusted.run_id,
            source_commit=trusted.source_commit,
            manifest_sha256=manifest_sha256,
            runtime_config_sha256=confirmed_runtime_config_sha256,
            preflight_report_sha256=preflight_report_sha256,
            factory_binding_sha256=factory_binding_sha256,
            resource_cleanup_upper_bound_seconds=(resource_cleanup_upper_bound_seconds),
            resource_cleanup_upper_bound_preimage=(resource_cleanup_upper_bound_preimage),
            resource_cleanup_upper_bound_sha256=resource_cleanup_upper_bound_sha256,
        )
        self._state = ExecutorStateV1.READY
        self._next_stage_index = 0
        self._receipts: list[StageExecutionReceiptV1] = []
        self._secret_leases_acquired = 0
        self._secret_leases_closed = 0
        self._sequence_started_ns: int | None = None
        self._execution_deadline_monotonic_ns: int | None = None
        self._cleanup_deadline_monotonic_ns: int | None = None
        self._cleanup_attempted = False
        self._cleanup_succeeded = False
        self._cleanup_evidence_preimage: bytes | None = None
        self._cleanup_status: str | None = None
        self._cleanup_wall_time_ms = 0
        self._current_stage_evidence_preimage: bytes | None = None
        self._terminal_result: R24SmokeSequenceResultV1 | None = None
        self._lock = threading.RLock()

    def _start_sequence(self) -> None:
        if self._sequence_started_ns is not None:
            return
        started_ns = time.monotonic_ns()
        cleanup_deadline_ns = min(
            self._authority_deadline_monotonic_ns,
            started_ns + self._manifest.max_sequence_wall_time_seconds * 1_000_000_000,
        )
        execution_deadline_ns = (
            cleanup_deadline_ns
            - self._manifest.max_resource_cleanup_wall_time_seconds * 1_000_000_000
        )
        if execution_deadline_ns <= started_ns:
            raise LiveRunContractError(
                "CLEANUP_RESERVE_UNAVAILABLE",
                "owner authority cannot preserve the cleanup window",
            )
        self._sequence_started_ns = started_ns
        self._execution_deadline_monotonic_ns = execution_deadline_ns
        self._cleanup_deadline_monotonic_ns = cleanup_deadline_ns

    def _remaining_counts(self) -> tuple[int, int, int]:
        return (
            self._manifest.max_sequence_actor_calls
            - sum(receipt.actor_calls for receipt in self._receipts),
            self._manifest.max_sequence_openai_calls
            - sum(receipt.openai_calls for receipt in self._receipts),
            self._manifest.max_sequence_cost_usd_micros
            - sum(receipt.cost_usd_micros for receipt in self._receipts),
        )

    def _context(self, *, deadline_ns: int, remaining_wall_time_ms: int) -> StageAdapterContextV1:
        actor, openai, cost = self._remaining_counts()
        return StageAdapterContextV1(
            manifest_sha256=self._manifest_sha256,
            sequence_execution_scope=SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY.value,
            sequence_scope_authority_sha256=self._manifest_sha256,
            run_id=self._manifest.run_id,
            source_commit=self._manifest.source_commit,
            remaining_actor_calls=max(0, actor),
            remaining_openai_calls=max(0, openai),
            remaining_cost_usd_micros=max(0, cost),
            remaining_wall_time_ms=max(0, remaining_wall_time_ms),
            authority_deadline_monotonic_ns=deadline_ns,
        )

    def _stage_context(self, stage_budget_seconds: int) -> StageAdapterContextV1:
        assert self._execution_deadline_monotonic_ns is not None
        now_ns = time.monotonic_ns()
        deadline_ns = min(
            self._execution_deadline_monotonic_ns,
            now_ns + stage_budget_seconds * 1_000_000_000,
        )
        if deadline_ns <= now_ns:
            raise _StageFailure("SMOKE_EXECUTION_DEADLINE_EXCEEDED")
        remaining_ms = max(0, (deadline_ns - now_ns + 999_999) // 1_000_000)
        return self._context(deadline_ns=deadline_ns, remaining_wall_time_ms=remaining_ms)

    def _cleanup_context(self) -> StageAdapterContextV1:
        assert self._cleanup_deadline_monotonic_ns is not None
        now_ns = time.monotonic_ns()
        remaining_ms = max(0, (self._cleanup_deadline_monotonic_ns - now_ns + 999_999) // 1_000_000)
        return self._context(
            deadline_ns=self._cleanup_deadline_monotonic_ns,
            remaining_wall_time_ms=remaining_ms,
        )

    @staticmethod
    def _elapsed_ms(started_ns: int, ended_ns: int) -> int:
        return max(0, (ended_ns - started_ns + 999_999) // 1_000_000)

    def _total_wall_time_ms(self) -> int:
        if self._sequence_started_ns is None:
            return 0
        return self._elapsed_ms(self._sequence_started_ns, time.monotonic_ns())

    def _synthetic_cleanup_failure(
        self,
        code: str,
        adapter_evidence_preimage: bytes | None,
    ) -> bytes:
        adapter_evidence: dict[str, object] | None = None
        adapter_evidence_sha256: str | None = None
        if adapter_evidence_preimage is not None:
            try:
                adapter_evidence = _smoke_evidence_object(
                    adapter_evidence_preimage, "cleanup failure"
                )
            except _StageFailure:
                adapter_evidence = None
            else:
                adapter_evidence_sha256 = hashlib.sha256(adapter_evidence_preimage).hexdigest()
        return _canonical_bytes(
            {
                "adapter_cleanup_evidence": adapter_evidence,
                "adapter_cleanup_evidence_sha256": adapter_evidence_sha256,
                "execution_scope": SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY.value,
                "failure_code": code,
                "factory_binding_sha256": self._factory_binding_sha256,
                "manifest_sha256": self._manifest_sha256,
                "preflight_report_sha256": self._preflight_report_sha256,
                "runtime_config_sha256": self._runtime_config_sha256,
                "schema_version": R24_SMOKE_EXECUTOR_BINDING_SCHEMA_VERSION,
                "status": "RETRY_REQUIRED",
            }
        )

    def _cleanup(self) -> bool:
        if self._cleanup_attempted:
            return self._cleanup_succeeded
        self._cleanup_attempted = True
        context = self._cleanup_context()
        started_ns = time.monotonic_ns()
        failure_code: str | None = None
        adapter_failure: bytes | None = None
        try:
            self._adapters.resources.cleanup(context)
        except Exception as exc:
            failure_code = _adapter_error_code(exc, "RESOURCE_CLEANUP_FAILED")
            try:
                adapter_failure = self._adapters.resources.failure_evidence_preimage(
                    RunStageV1.MAI_LIVE_SMOKE
                )
            except Exception:
                adapter_failure = None
        ended_ns = time.monotonic_ns()
        self._cleanup_wall_time_ms = self._elapsed_ms(started_ns, ended_ns)
        assert self._cleanup_deadline_monotonic_ns is not None
        success_evidence: bytes | None = None
        if failure_code is None:
            try:
                success_evidence = self._adapters.resources.cleanup_success_evidence_preimage()
                if success_evidence is None:
                    raise ValueError("cleanup success evidence is absent")
                _smoke_evidence_object(success_evidence, "cleanup success")
            except Exception:
                failure_code = "RESOURCE_CLEANUP_EVIDENCE_INVALID"
        if failure_code is None and (
            ended_ns >= self._cleanup_deadline_monotonic_ns
            or self._cleanup_wall_time_ms
            > self._manifest.max_resource_cleanup_wall_time_seconds * 1000
        ):
            failure_code = "RESOURCE_CLEANUP_DEADLINE_EXCEEDED"
            adapter_failure = success_evidence
        if failure_code is None:
            assert success_evidence is not None
            self._cleanup_succeeded = True
            self._cleanup_status = "SUCCEEDED"
            self._cleanup_evidence_preimage = success_evidence
            return True
        self._cleanup_succeeded = False
        self._cleanup_status = "RETRY_REQUIRED"
        self._cleanup_evidence_preimage = self._synthetic_cleanup_failure(
            failure_code or "RESOURCE_CLEANUP_FAILED", adapter_failure
        )
        return False

    def _smoke_plan(self, host: PilotHostV1) -> HostLiveSmokePlanV1:
        return next(plan for plan in self._manifest.smoke_plans if plan.host is host)

    def _resource(self, host: PilotHostV1) -> SnapshotResourceV1:
        return next(
            resource for resource in self._manifest.actor_resources if resource.host is host
        )

    def _run_with_broker(
        self,
        *,
        stage: RunStageV1,
        host: PilotHostV1,
        context: StageAdapterContextV1,
    ) -> AdapterStageResultV1:
        try:
            broker = self._adapters.secret_leases.acquire(
                self._manifest.secret,
                manifest_sha256=self._manifest_sha256,
            )
        except Exception:
            raise _StageFailure("SECRET_LEASE_UNAVAILABLE") from None
        if broker.manifest_sha256 != self._manifest_sha256 or (
            broker.environment_key != self._manifest.secret.environment_key
        ):
            try:
                broker.close()
            except Exception:
                pass
            raise _StageFailure("INVALID_CASE_AUTHORITY_BROKER")
        self._secret_leases_acquired += 1
        result: AdapterStageResultV1 | None = None
        adapter_error: Exception | None = None
        try:
            result = self._adapters.smoke.run_host(
                host,
                self._smoke_plan(host),
                self._resource(host),
                self._manifest.openai_stages,
                context,
                broker,
            )
        except Exception as exc:
            adapter_error = exc
        adapter_failure_evidence: bytes | None = None
        if adapter_error is not None:
            try:
                adapter_failure_evidence = self._adapters.smoke.failure_evidence_preimage(stage)
            except Exception:
                adapter_failure_evidence = None
        try:
            broker.close()
            self._secret_leases_closed += 1
        except Exception:
            retained_evidence = (
                result.evidence_preimage
                if type(result) is AdapterStageResultV1
                else adapter_failure_evidence
            )
            raise _StageFailure("CASE_AUTHORITY_BROKER_CLOSE_FAILED", retained_evidence) from None
        if adapter_error is not None:
            raise _StageFailure(
                _adapter_error_code(adapter_error, "STAGE_ADAPTER_FAILED"),
                adapter_failure_evidence,
            ) from None
        if type(result) is not AdapterStageResultV1:
            raise _StageFailure("INVALID_ADAPTER_RESULT")
        return result

    def _validate_resource_result(
        self, result: AdapterStageResultV1, elapsed_ms: int
    ) -> StageExecutionReceiptV1:
        if (
            result.stage is not RunStageV1.RESOURCE_PREFLIGHT
            or result.manifest_sha256 != self._manifest_sha256
            or result.completed_units != ("resources",)
            or any(
                value != 0
                for value in (
                    result.actor_calls,
                    result.openai_calls,
                    result.actor_actions,
                    result.cost_usd_micros,
                )
            )
            or result.provider_final_request_proven
            or elapsed_ms > self._manifest.max_resource_preflight_wall_time_seconds * 1000
        ):
            raise _StageFailure("STAGE_BUDGET_OR_CENSUS_MISMATCH")
        return StageExecutionReceiptV1(
            stage=RunStageV1.RESOURCE_PREFLIGHT,
            manifest_sha256=self._manifest_sha256,
            passed=True,
            evidence_sha256=result.evidence_sha256,
            actor_calls=0,
            openai_calls=0,
            actor_actions=0,
            cost_usd_micros=0,
            wall_time_ms=elapsed_ms,
            completed_units=result.completed_units,
            provider_final_request_proven=False,
        )

    def _validate_smoke_result(
        self,
        *,
        stage: RunStageV1,
        host: PilotHostV1,
        result: AdapterStageResultV1,
        elapsed_ms: int,
        additional_wall_time_seconds: int = 0,
    ) -> StageExecutionReceiptV1:
        plan = self._smoke_plan(host)
        expected_units = tuple(f"{host.value}:{case.mode.value}" for case in plan.cases)
        minimum_openai_calls = sum(0 if case.mode is SmokeModeV1.OFF else 2 for case in plan.cases)
        maximum_openai_calls = sum(case.max_openai_calls for case in plan.cases)
        if (
            result.stage is not stage
            or result.manifest_sha256 != self._manifest_sha256
            or result.completed_units != expected_units
            or result.actor_calls != sum(case.max_actor_calls for case in plan.cases)
            or not minimum_openai_calls <= result.openai_calls <= maximum_openai_calls
            or result.actor_actions != 0
            or result.cost_usd_micros > sum(case.max_cost_usd_micros for case in plan.cases)
            or elapsed_ms
            > (
                sum(case.max_wall_time_seconds for case in plan.cases)
                + additional_wall_time_seconds
            )
            * 1000
            or not result.provider_final_request_proven
        ):
            raise _StageFailure("STAGE_BUDGET_OR_CENSUS_MISMATCH")
        receipt = StageExecutionReceiptV1(
            stage=stage,
            manifest_sha256=self._manifest_sha256,
            passed=True,
            evidence_sha256=result.evidence_sha256,
            actor_calls=result.actor_calls,
            openai_calls=result.openai_calls,
            actor_actions=0,
            cost_usd_micros=result.cost_usd_micros,
            wall_time_ms=elapsed_ms,
            completed_units=expected_units,
            provider_final_request_proven=True,
        )
        projected = (*self._receipts, receipt)
        if (
            sum(item.actor_calls for item in projected) > self._manifest.max_sequence_actor_calls
            or sum(item.openai_calls for item in projected)
            > self._manifest.max_sequence_openai_calls
            or sum(item.cost_usd_micros for item in projected)
            > self._manifest.max_sequence_cost_usd_micros
            or sum(item.wall_time_ms for item in projected)
            > self._manifest.max_sequence_wall_time_seconds * 1000
        ):
            raise _StageFailure("SEQUENCE_BUDGET_EXCEEDED")
        return receipt

    def _mai_failure_evidence(
        self,
        *,
        failure_code: str,
        status: str,
        handoff_preimage: bytes | None,
        smoke_preimage: bytes | None,
    ) -> bytes:
        handoff = (
            None
            if handoff_preimage is None
            else _smoke_evidence_object(handoff_preimage, "handoff")
        )
        smoke = (
            None if smoke_preimage is None else _smoke_evidence_object(smoke_preimage, "MAI smoke")
        )
        return _canonical_bytes(
            {
                "execution_scope": SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY.value,
                "factory_binding_sha256": self._factory_binding_sha256,
                "failure_code": failure_code,
                "handoff_evidence": handoff,
                "handoff_evidence_sha256": (
                    None
                    if handoff_preimage is None
                    else hashlib.sha256(handoff_preimage).hexdigest()
                ),
                "manifest_sha256": self._manifest_sha256,
                "preflight_report_sha256": self._preflight_report_sha256,
                "runtime_config_sha256": self._runtime_config_sha256,
                "schema_version": R24_SMOKE_MAI_FAILURE_EVIDENCE_SCHEMA_VERSION,
                "smoke_evidence": smoke,
                "smoke_evidence_sha256": (
                    None if smoke_preimage is None else hashlib.sha256(smoke_preimage).hexdigest()
                ),
                "status": status,
            }
        )

    def _mai_terminal_result(
        self,
        *,
        handoff: AdapterStageResultV1,
        smoke: AdapterStageResultV1,
        cleanup_preimage: bytes,
    ) -> AdapterStageResultV1:
        handoff_evidence = _smoke_evidence_object(handoff.evidence_preimage, "handoff")
        smoke_evidence = _smoke_evidence_object(smoke.evidence_preimage, "MAI smoke")
        cleanup_evidence = _smoke_evidence_object(cleanup_preimage, "cleanup")
        preimage = _canonical_bytes(
            {
                "execution_scope": SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY.value,
                "factory_binding_sha256": self._factory_binding_sha256,
                "handoff_evidence": handoff_evidence,
                "handoff_evidence_sha256": handoff.evidence_sha256,
                "manifest_sha256": self._manifest_sha256,
                "preflight_report_sha256": self._preflight_report_sha256,
                "resource_cleanup_evidence": cleanup_evidence,
                "resource_cleanup_evidence_sha256": hashlib.sha256(cleanup_preimage).hexdigest(),
                "runtime_config_sha256": self._runtime_config_sha256,
                "schema_version": R24_SMOKE_MAI_TERMINAL_EVIDENCE_SCHEMA_VERSION,
                "smoke_evidence": smoke_evidence,
                "smoke_evidence_sha256": smoke.evidence_sha256,
                "status": "COMPLETED_AND_CLEANED",
            }
        )
        return AdapterStageResultV1(
            stage=RunStageV1.MAI_LIVE_SMOKE,
            manifest_sha256=self._manifest_sha256,
            evidence_sha256=hashlib.sha256(preimage).hexdigest(),
            evidence_preimage=preimage,
            actor_calls=smoke.actor_calls,
            openai_calls=smoke.openai_calls,
            actor_actions=smoke.actor_actions,
            cost_usd_micros=smoke.cost_usd_micros,
            completed_units=smoke.completed_units,
            provider_final_request_proven=smoke.provider_final_request_proven,
        )

    def _build_terminal_result(
        self,
        *,
        status: SequenceStatusV1,
        failed_stage: RunStageV1 | None,
        failure_code: str | None,
        cleanup_status: str,
        cleanup_sha256: str,
        successful_output_committed: bool,
    ) -> R24SmokeSequenceResultV1:
        return R24SmokeSequenceResultV1(
            schema_version=R24_SMOKE_SEQUENCE_RESULT_SCHEMA_VERSION,
            execution_scope=SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY,
            run_id=self._manifest.run_id,
            source_commit=self._manifest.source_commit,
            manifest_sha256=self._manifest_sha256,
            runtime_config_sha256=self._runtime_config_sha256,
            preflight_report_sha256=self._preflight_report_sha256,
            factory_binding_sha256=self._factory_binding_sha256,
            status=status,
            completed_stages=tuple(receipt.stage for receipt in self._receipts),
            receipts=tuple(self._receipts),
            pilot_executed=False,
            resource_cleanup_status=cleanup_status,
            resource_cleanup_evidence_sha256=cleanup_sha256,
            resource_cleanup_upper_bound_seconds=(self._resource_cleanup_upper_bound_seconds),
            resource_cleanup_upper_bound_preimage=(self._resource_cleanup_upper_bound_preimage),
            resource_cleanup_upper_bound_sha256=(self._resource_cleanup_upper_bound_sha256),
            total_wall_time_ms=self._total_wall_time_ms(),
            terminal_output_published=True,
            successful_output_committed=successful_output_committed,
            failed_stage=failed_stage,
            failure_code=failure_code,
        )

    def _abort(
        self,
        code: str,
        *,
        failed_stage: RunStageV1,
        failure_evidence_preimage: bytes | None,
    ) -> None:
        cleanup_ok = self._cleanup()
        assert self._cleanup_evidence_preimage is not None
        assert self._cleanup_status is not None
        terminal_code = code if cleanup_ok else "EXECUTOR_CLEANUP_FAILED"
        result = self._build_terminal_result(
            status=SequenceStatusV1.FAILED,
            failed_stage=failed_stage,
            failure_code=terminal_code,
            cleanup_status=self._cleanup_status,
            cleanup_sha256=hashlib.sha256(self._cleanup_evidence_preimage).hexdigest(),
            successful_output_committed=False,
        )
        try:
            self._output.fail(
                result=result,
                stage_failure_evidence_preimage=failure_evidence_preimage,
                resource_cleanup_evidence_preimage=self._cleanup_evidence_preimage,
            )
        except Exception:
            try:
                self._output.preserve_failure_recovery(terminal_code)
            except Exception:
                pass
            self._state = ExecutorStateV1.FAILED
            raise LiveRunContractError(
                "EXECUTOR_FAILURE_PUBLICATION_FAILED",
                "smoke failure evidence could not be durably published",
            ) from None
        self._terminal_result = result
        self._state = ExecutorStateV1.FAILED
        raise LiveRunContractError(terminal_code, "R2.4 smoke executor stopped fail-closed")

    def _run_resource(self) -> StageExecutionReceiptV1:
        context = self._stage_context(self._manifest.max_resource_preflight_wall_time_seconds)
        started_ns = time.monotonic_ns()
        try:
            result = self._adapters.resources.prepare(self._manifest.actor_resources, context)
        except Exception as exc:
            try:
                evidence = self._adapters.resources.failure_evidence_preimage(
                    RunStageV1.RESOURCE_PREFLIGHT
                )
            except Exception:
                evidence = None
            raise _StageFailure(
                _adapter_error_code(exc, "RESOURCE_ADAPTER_FAILED"), evidence
            ) from None
        ended_ns = time.monotonic_ns()
        if type(result) is not AdapterStageResultV1:
            raise _StageFailure("INVALID_ADAPTER_RESULT")
        self._current_stage_evidence_preimage = result.evidence_preimage
        if ended_ns >= context.authority_deadline_monotonic_ns:
            raise _StageFailure("RESOURCE_PREFLIGHT_DEADLINE_EXCEEDED", result.evidence_preimage)
        receipt = self._validate_resource_result(result, self._elapsed_ms(started_ns, ended_ns))
        try:
            self._output.record(receipt, result.evidence_preimage)
        except Exception:
            raise _StageFailure("OUTPUT_TRANSACTION_FAILED", result.evidence_preimage) from None
        return receipt

    def _run_qwen(self) -> StageExecutionReceiptV1:
        plan = self._smoke_plan(PilotHostV1.QWEN3_VL)
        context = self._stage_context(sum(case.max_wall_time_seconds for case in plan.cases))
        started_ns = time.monotonic_ns()
        result = self._run_with_broker(
            stage=RunStageV1.QWEN_LIVE_SMOKE,
            host=PilotHostV1.QWEN3_VL,
            context=context,
        )
        self._current_stage_evidence_preimage = result.evidence_preimage
        ended_ns = time.monotonic_ns()
        if ended_ns >= context.authority_deadline_monotonic_ns:
            raise _StageFailure("QWEN_SMOKE_DEADLINE_EXCEEDED", result.evidence_preimage)
        receipt = self._validate_smoke_result(
            stage=RunStageV1.QWEN_LIVE_SMOKE,
            host=PilotHostV1.QWEN3_VL,
            result=result,
            elapsed_ms=self._elapsed_ms(started_ns, ended_ns),
        )
        try:
            self._output.record(receipt, result.evidence_preimage)
        except Exception:
            raise _StageFailure("OUTPUT_TRANSACTION_FAILED", result.evidence_preimage) from None
        return receipt

    def _run_mai(self) -> StageExecutionReceiptV1:
        terminal_started_ns = time.monotonic_ns()
        handoff_context = self._stage_context(
            self._manifest.max_qwen_to_mai_handoff_wall_time_seconds
        )
        handoff_started_ns = time.monotonic_ns()
        try:
            handoff = self._adapters.resources.handoff_to_mai(handoff_context)
        except Exception as exc:
            try:
                evidence = self._adapters.resources.failure_evidence_preimage(
                    RunStageV1.MAI_LIVE_SMOKE
                )
            except Exception:
                evidence = None
            raise _StageFailure(
                _adapter_error_code(exc, "RESOURCE_HANDOFF_FAILED"), evidence
            ) from None
        handoff_ended_ns = time.monotonic_ns()
        if type(handoff) is not AdapterStageResultV1:
            raise _StageFailure("INVALID_HANDOFF_RESULT")
        if (
            handoff.stage is not RunStageV1.MAI_LIVE_SMOKE
            or handoff.manifest_sha256 != self._manifest_sha256
            or handoff.actor_calls != 0
            or handoff.openai_calls != 0
            or handoff.actor_actions != 0
            or handoff.cost_usd_micros != 0
            or handoff.completed_units != ("resource-handoff:QWEN3_VL:MAI_UI",)
            or handoff.provider_final_request_proven
        ):
            raise _StageFailure("INVALID_HANDOFF_RESULT", handoff.evidence_preimage)
        if handoff_ended_ns >= handoff_context.authority_deadline_monotonic_ns or (
            self._elapsed_ms(handoff_started_ns, handoff_ended_ns)
            > self._manifest.max_qwen_to_mai_handoff_wall_time_seconds * 1000
        ):
            raise _StageFailure("RESOURCE_HANDOFF_DEADLINE_EXCEEDED", handoff.evidence_preimage)
        plan = self._smoke_plan(PilotHostV1.MAI_UI)
        smoke_context = self._stage_context(sum(case.max_wall_time_seconds for case in plan.cases))
        smoke_started_ns = time.monotonic_ns()
        try:
            smoke = self._run_with_broker(
                stage=RunStageV1.MAI_LIVE_SMOKE,
                host=PilotHostV1.MAI_UI,
                context=smoke_context,
            )
        except _StageFailure as exc:
            combined = self._mai_failure_evidence(
                failure_code=exc.code,
                status="MAI_SMOKE_FAILED_AFTER_HANDOFF",
                handoff_preimage=handoff.evidence_preimage,
                smoke_preimage=exc.evidence_preimage,
            )
            raise _StageFailure(exc.code, combined) from None
        smoke_ended_ns = time.monotonic_ns()
        if smoke_ended_ns >= smoke_context.authority_deadline_monotonic_ns:
            combined = self._mai_failure_evidence(
                failure_code="MAI_SMOKE_DEADLINE_EXCEEDED",
                status="MAI_SMOKE_FAILED_AFTER_HANDOFF",
                handoff_preimage=handoff.evidence_preimage,
                smoke_preimage=smoke.evidence_preimage,
            )
            raise _StageFailure("MAI_SMOKE_DEADLINE_EXCEEDED", combined)
        smoke_elapsed_ms = self._elapsed_ms(smoke_started_ns, smoke_ended_ns)
        try:
            self._validate_smoke_result(
                stage=RunStageV1.MAI_LIVE_SMOKE,
                host=PilotHostV1.MAI_UI,
                result=smoke,
                elapsed_ms=smoke_elapsed_ms,
            )
        except _StageFailure as exc:
            combined = self._mai_failure_evidence(
                failure_code=exc.code,
                status="MAI_SMOKE_ADMISSION_FAILED_AFTER_HANDOFF",
                handoff_preimage=handoff.evidence_preimage,
                smoke_preimage=smoke.evidence_preimage,
            )
            raise _StageFailure(exc.code, combined) from None
        pending_cleanup = self._mai_failure_evidence(
            failure_code="RESOURCE_CLEANUP_FAILED",
            status="MAI_SMOKE_COMPLETED_CLEANUP_PENDING",
            handoff_preimage=handoff.evidence_preimage,
            smoke_preimage=smoke.evidence_preimage,
        )
        if not self._cleanup():
            raise _StageFailure("EXECUTOR_CLEANUP_FAILED", pending_cleanup)
        assert self._cleanup_evidence_preimage is not None
        terminal = self._mai_terminal_result(
            handoff=handoff,
            smoke=smoke,
            cleanup_preimage=self._cleanup_evidence_preimage,
        )
        self._current_stage_evidence_preimage = terminal.evidence_preimage
        try:
            cleanup_sha256 = self._output.record_cleanup_success(self._cleanup_evidence_preimage)
        except Exception:
            raise _StageFailure("OUTPUT_TRANSACTION_FAILED", terminal.evidence_preimage) from None
        total_elapsed_ms = self._elapsed_ms(terminal_started_ns, time.monotonic_ns())
        receipt = self._validate_smoke_result(
            stage=RunStageV1.MAI_LIVE_SMOKE,
            host=PilotHostV1.MAI_UI,
            result=terminal,
            elapsed_ms=total_elapsed_ms,
            additional_wall_time_seconds=(
                self._manifest.max_qwen_to_mai_handoff_wall_time_seconds
                + self._manifest.max_resource_cleanup_wall_time_seconds
            ),
        )
        if cleanup_sha256 != hashlib.sha256(self._cleanup_evidence_preimage).hexdigest():
            raise _StageFailure("RESOURCE_CLEANUP_EVIDENCE_INVALID", terminal.evidence_preimage)
        try:
            self._output.record(receipt, terminal.evidence_preimage)
        except Exception:
            raise _StageFailure("OUTPUT_TRANSACTION_FAILED", terminal.evidence_preimage) from None
        return receipt

    def run_stage(
        self,
        stage: RunStageV1,
        manifest: R24SmokeRunAuthorityManifestV1,
    ) -> StageExecutionReceiptV1:
        with self._lock:
            if self._state in {ExecutorStateV1.COMPLETE, ExecutorStateV1.FAILED}:
                raise LiveRunContractError("EXECUTOR_STOPPED", "smoke executor is terminal")
            if (
                type(stage) is not RunStageV1
                or type(manifest) is not R24SmokeRunAuthorityManifestV1
            ):
                raise LiveRunContractError("UNTRUSTED_EXECUTOR_INPUT", "smoke input type differs")
            if stage is RunStageV1.R25_PILOT:
                if self._sequence_started_ns is not None:
                    expected = self._STAGES[self._next_stage_index]
                    evidence = _canonical_bytes(
                        {
                            "attempted_stage": RunStageV1.R25_PILOT.value,
                            "execution_scope": (SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY.value),
                            "failure_code": "R25_STAGE_FORBIDDEN",
                            "manifest_sha256": self._manifest_sha256,
                            "schema_version": R24_SMOKE_EXECUTOR_BINDING_SCHEMA_VERSION,
                            "status": "REJECTED_BEFORE_ADAPTER",
                        }
                    )
                    self._abort(
                        "R25_STAGE_FORBIDDEN",
                        failed_stage=expected,
                        failure_evidence_preimage=evidence,
                    )
                raise LiveRunContractError("R25_STAGE_FORBIDDEN", "R2.5 is outside smoke authority")
            if smoke_authority_manifest_sha256(manifest) != self._manifest_sha256:
                if self._sequence_started_ns is None:
                    raise LiveRunContractError(
                        "MANIFEST_BINDING_MISMATCH", "smoke authority hash differs"
                    )
                self._abort(
                    "MANIFEST_BINDING_MISMATCH",
                    failed_stage=stage,
                    failure_evidence_preimage=None,
                )
            expected = self._STAGES[self._next_stage_index]
            if stage is not expected:
                if self._sequence_started_ns is None:
                    raise LiveRunContractError("STAGE_ORDER_VIOLATION", "smoke stage order differs")
                self._abort(
                    "STAGE_ORDER_VIOLATION",
                    failed_stage=stage,
                    failure_evidence_preimage=None,
                )
            if self._sequence_started_ns is None:
                self._start_sequence()
                try:
                    self._output.begin()
                except Exception:
                    raise LiveRunContractError(
                        "OUTPUT_TRANSACTION_FAILED", "smoke output admission failed"
                    ) from None
            assert self._execution_deadline_monotonic_ns is not None
            if time.monotonic_ns() >= self._execution_deadline_monotonic_ns:
                self._abort(
                    "SMOKE_EXECUTION_DEADLINE_EXCEEDED",
                    failed_stage=stage,
                    failure_evidence_preimage=None,
                )
            self._state = ExecutorStateV1.RUNNING
            self._current_stage_evidence_preimage = None
            try:
                if stage is RunStageV1.RESOURCE_PREFLIGHT:
                    receipt = self._run_resource()
                elif stage is RunStageV1.QWEN_LIVE_SMOKE:
                    receipt = self._run_qwen()
                else:
                    receipt = self._run_mai()
                self._receipts.append(receipt)
                self._next_stage_index += 1
                if stage is RunStageV1.MAI_LIVE_SMOKE:
                    assert self._cleanup_evidence_preimage is not None
                    result = self._build_terminal_result(
                        status=SequenceStatusV1.COMPLETE,
                        failed_stage=None,
                        failure_code=None,
                        cleanup_status="SUCCEEDED",
                        cleanup_sha256=hashlib.sha256(self._cleanup_evidence_preimage).hexdigest(),
                        successful_output_committed=True,
                    )
                    try:
                        self._output.commit(result)
                    except Exception:
                        self._abort(
                            "OUTPUT_TRANSACTION_FAILED",
                            failed_stage=RunStageV1.MAI_LIVE_SMOKE,
                            failure_evidence_preimage=(self._current_stage_evidence_preimage),
                        )
                    self._terminal_result = result
                    self._state = ExecutorStateV1.COMPLETE
                return receipt
            except _StageFailure as exc:
                self._abort(
                    exc.code,
                    failed_stage=stage,
                    failure_evidence_preimage=(
                        exc.evidence_preimage
                        if exc.evidence_preimage is not None
                        else self._current_stage_evidence_preimage
                    ),
                )
            except LiveRunContractError:
                raise
            except Exception:
                self._abort(
                    "EXECUTOR_INTERNAL_FAILURE",
                    failed_stage=stage,
                    failure_evidence_preimage=self._current_stage_evidence_preimage,
                )
        raise AssertionError("unreachable R2.4 smoke executor state")

    def execute(self, manifest: R24SmokeRunAuthorityManifestV1) -> R24SmokeSequenceResultV1:
        for stage in self._STAGES:
            try:
                self.run_stage(stage, manifest)
            except LiveRunContractError:
                if self._terminal_result is not None:
                    return self._terminal_result
                raise
        if self._terminal_result is None:
            raise LiveRunContractError(
                "TERMINAL_RESULT_MISSING", "smoke sequence did not publish a terminal result"
            )
        return self._terminal_result

    @property
    def terminal_result(self) -> R24SmokeSequenceResultV1 | None:
        with self._lock:
            return self._terminal_result

    @property
    def census(self) -> ExecutorCensusV1:
        with self._lock:
            return ExecutorCensusV1(
                state=self._state,
                manifest_sha256=self._manifest_sha256,
                completed_stages=tuple(receipt.stage for receipt in self._receipts),
                actor_calls=sum(receipt.actor_calls for receipt in self._receipts),
                openai_calls=sum(receipt.openai_calls for receipt in self._receipts),
                actor_actions=sum(receipt.actor_actions for receipt in self._receipts),
                cost_usd_micros=sum(receipt.cost_usd_micros for receipt in self._receipts),
                wall_time_ms=sum(receipt.wall_time_ms for receipt in self._receipts),
                secret_leases_acquired=self._secret_leases_acquired,
                secret_leases_closed=self._secret_leases_closed,
                cleanup_attempted=self._cleanup_attempted,
                cleanup_succeeded=self._cleanup_succeeded,
                output_committed=self._output.committed,
            )


class ProductionR24SmokeExecutorV1(_R24SmokeExecutorCoreV1):
    """Production R2.4-only executor created from exact post-preflight dependencies."""

    def __init__(
        self,
        manifest: R24SmokeRunAuthorityManifestV1,
        *,
        confirmed_manifest_sha256: str,
        preflight_report_sha256: str,
        factory_binding_sha256: str,
        confirmed_runtime_config_sha256: str,
        resource_cleanup_upper_bound_seconds: int,
        resource_cleanup_upper_bound_preimage: bytes,
        resource_cleanup_upper_bound_sha256: str,
        repository_root: Path,
        module_owned_adapters: object,
        seal: object | None = None,
    ) -> None:
        if (
            seal is not _PRODUCTION_EXECUTOR_SEAL
            or type(module_owned_adapters) is not _R24SmokeAdapterBundleV1
            or not module_owned_adapters.production
        ):
            raise LiveRunContractError(
                "PRODUCTION_ADAPTERS_UNAVAILABLE",
                "reviewed R2.4 smoke-only production adapters are required",
            )
        super().__init__(
            manifest,
            confirmed_manifest_sha256=confirmed_manifest_sha256,
            preflight_report_sha256=preflight_report_sha256,
            factory_binding_sha256=factory_binding_sha256,
            confirmed_runtime_config_sha256=confirmed_runtime_config_sha256,
            resource_cleanup_upper_bound_seconds=(resource_cleanup_upper_bound_seconds),
            resource_cleanup_upper_bound_preimage=(resource_cleanup_upper_bound_preimage),
            resource_cleanup_upper_bound_sha256=resource_cleanup_upper_bound_sha256,
            repository_root=repository_root,
            adapters=module_owned_adapters,
        )


class _CpuAttemptSecretLeaseV1:
    __slots__ = ("_closed", "_environment_key", "_manifest_sha256")

    def __init__(self, manifest_sha256: str, environment_key: str) -> None:
        self._manifest_sha256 = manifest_sha256
        self._environment_key = environment_key
        self._closed = False

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    @property
    def environment_key(self) -> str:
        return self._environment_key

    def close(self) -> None:
        if self._closed:
            raise RuntimeError("lease already closed")
        self._closed = True


class _CpuLeaseV1:
    """CPU no-secret case broker retained under its compatibility name."""

    __slots__ = ("_closed", "_environment_key", "_fail_close", "_manifest_sha256")

    def __init__(
        self, manifest_sha256: str, environment_key: str, *, fail_close: bool = False
    ) -> None:
        self._manifest_sha256 = manifest_sha256
        self._environment_key = environment_key
        self._fail_close = fail_close
        self._closed = False

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    @property
    def environment_key(self) -> str:
        return self._environment_key

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
        if self._closed:
            raise RuntimeError("case authority broker is closed")
        subject = (
            f"{self._manifest_sha256}\0{stage.value}\0{host.value}\0{mode.value}\0"
            f"{case_id}\0{task_id}\0{task_parameters_sha256}\0{reset_seed}\0"
            f"{actor_call_index}\0{request_sha256}"
        )
        lease_sha256 = hashlib.sha256(f"CPU_CASE_LEASE\0{subject}".encode()).hexdigest()
        return CaseExecutionLeaseBindingV1(
            manifest_sha256=self._manifest_sha256,
            preflight_report_sha256=hashlib.sha256(
                f"CPU_PREFLIGHT\0{self._manifest_sha256}".encode()
            ).hexdigest(),
            factory_binding_sha256=hashlib.sha256(
                f"CPU_FACTORY\0{self._manifest_sha256}".encode()
            ).hexdigest(),
            pricing_binding_sha256=hashlib.sha256(
                f"CPU_PRICING\0{self._manifest_sha256}".encode()
            ).hexdigest(),
            case_execution_lease_sha256=lease_sha256,
            execution_scope="CPU_TEST_LOCAL",
            openai_stage_set_sha256=hashlib.sha256(
                f"CPU_OPENAI_STAGE_SET\0{self._manifest_sha256}".encode()
            ).hexdigest(),
            stage=stage,
            host=host,
            mode=mode,
            case_id=case_id,
            task_id=task_id,
            task_parameters_sha256=task_parameters_sha256,
            reset_seed=reset_seed,
            actor_call_index=actor_call_index,
            request_sha256=request_sha256,
            issued_at_utc="2026-09-03T00:00:00Z",
            expires_at_utc="2026-09-03T00:01:00Z",
        )

    def acquire_secret_lease(self, case_lease: CaseExecutionLeaseBindingV1) -> SecureSecretLeaseV1:
        if (
            self._closed
            or type(case_lease) is not CaseExecutionLeaseBindingV1
            or case_lease.manifest_sha256 != self._manifest_sha256
        ):
            raise RuntimeError("CPU case lease differs")
        return _CpuAttemptSecretLeaseV1(self._manifest_sha256, self._environment_key)

    def close(self) -> None:
        if self._closed:
            raise RuntimeError("broker already closed")
        if self._fail_close:
            raise RuntimeError("CPU broker close fixture failed")
        self._closed = True


class _CpuSecretLeaseProviderV1:
    def __init__(self, fault: CpuTestFaultV1) -> None:
        self._fault = fault
        self._acquisitions = 0

    def acquire(
        self,
        reference: SecretFileReferenceV1,
        *,
        manifest_sha256: str,
    ) -> CaseAuthorityBrokerV1:
        if self._fault is CpuTestFaultV1.SECRET_LEASE_UNAVAILABLE:
            raise RuntimeError("CPU lease unavailable")
        self._acquisitions += 1
        fail_close = (
            (
                self._fault is CpuTestFaultV1.QWEN_CASE_BROKER_CLOSE_FAILURE
                and self._acquisitions == 1
            )
            or (
                self._fault is CpuTestFaultV1.MAI_CASE_BROKER_CLOSE_FAILURE
                and self._acquisitions == 2
            )
            or (
                self._fault is CpuTestFaultV1.PILOT_CASE_BROKER_CLOSE_FAILURE
                and self._acquisitions == 3
            )
        )
        return _CpuLeaseV1(
            manifest_sha256,
            reference.environment_key,
            fail_close=fail_close,
        )


def _cpu_evidence(manifest_sha256: str, stage: RunStageV1) -> bytes:
    return _canonical_bytes(
        {
            "execution_scope": "CPU_TEST_LOCAL",
            "manifest_sha256": manifest_sha256,
            "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
            "stage": stage.value,
        }
    )


def _cpu_failure_evidence(stage: RunStageV1, code: str) -> bytes:
    return _canonical_bytes(
        {
            "execution_scope": "CPU_TEST_LOCAL",
            "failure_code": code,
            "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
            "stage": stage.value,
            "status": "FAILED",
        }
    )


class _CpuResourceAdapterV1:
    def __init__(self, fault: CpuTestFaultV1) -> None:
        self._fault = fault
        self._failure_evidence: bytes | None = None
        self._cleanup_success_evidence: bytes | None = None

    def prepare(
        self,
        resources: tuple[SnapshotResourceV1, ...],
        context: StageAdapterContextV1,
    ) -> AdapterStageResultV1:
        if tuple(resource.host for resource in resources) != (
            PilotHostV1.QWEN3_VL,
            PilotHostV1.MAI_UI,
        ):
            raise RuntimeError("CPU resource fixture differs")
        evidence = _cpu_evidence(context.manifest_sha256, RunStageV1.RESOURCE_PREFLIGHT)
        return AdapterStageResultV1(
            stage=RunStageV1.RESOURCE_PREFLIGHT,
            manifest_sha256=context.manifest_sha256,
            evidence_sha256=hashlib.sha256(evidence).hexdigest(),
            evidence_preimage=evidence,
            actor_calls=0,
            openai_calls=0,
            actor_actions=0,
            cost_usd_micros=0,
            completed_units=("resources",),
            provider_final_request_proven=False,
        )

    def cleanup(self, context: StageAdapterContextV1) -> None:
        if self._fault in {
            CpuTestFaultV1.RESOURCE_CLEANUP_FAILURE,
            CpuTestFaultV1.QWEN_SMOKE_AND_RESOURCE_CLEANUP_FAILURE,
        }:
            residual = {
                "backend_container_id": "cpu-stubborn-container",
                "model_pids": [10_000, 10_001],
            }
            self._failure_evidence = _canonical_bytes(
                {
                    "cleanup_failure_code": "RESOURCE_CLEANUP_FAILED",
                    "cleanup_status": "RETRY_REQUIRED",
                    "execution_scope": "CPU_TEST_LOCAL",
                    "failure_code": "RESOURCE_CLEANUP_FAILED",
                    "manifest_sha256": context.manifest_sha256,
                    "residual_capabilities": residual,
                    "residual_capabilities_sha256": hashlib.sha256(
                        _canonical_bytes(residual)
                    ).hexdigest(),
                    "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
                    "stage": RunStageV1.RESOURCE_PREFLIGHT.value,
                    "status": "FAILED_CLEANUP_RETRY_REQUIRED",
                }
            )
            raise RuntimeError("CPU cleanup fixture failed")
        self._cleanup_success_evidence = _canonical_bytes(
            {
                "cleanup_status": "SUCCEEDED",
                "execution_scope": context.sequence_execution_scope,
                "manifest_sha256": context.manifest_sha256,
                "schema_version": (
                    "mobileworld.runtime.sentinel-r2.4-resource-cleanup-evidence/v1"
                ),
                "sequence_scope_authority_sha256": (context.sequence_scope_authority_sha256),
            }
        )

    def handoff_to_mai(self, context: StageAdapterContextV1) -> AdapterStageResultV1:
        if self._fault is CpuTestFaultV1.QWEN_TO_MAI_HANDOFF_FAILURE:
            self._failure_evidence = _canonical_bytes(
                {
                    "execution_scope": context.sequence_execution_scope,
                    "failure_code": "RESOURCE_HANDOFF_FAILED",
                    "manifest_sha256": context.manifest_sha256,
                    "schema_version": (
                        "mobileworld.runtime.sentinel-r2.4-model-handoff-evidence/v1"
                    ),
                    "source_host": PilotHostV1.QWEN3_VL.value,
                    "status": "FAILED_CLEANUP_REQUIRED",
                    "target_host": PilotHostV1.MAI_UI.value,
                }
            )
            raise RuntimeError("CPU handoff fixture failed")
        evidence = _canonical_bytes(
            {
                "execution_scope": context.sequence_execution_scope,
                "manifest_sha256": context.manifest_sha256,
                "schema_version": "mobileworld.runtime.sentinel-r2.4-model-handoff-evidence/v1",
                "source_host": PilotHostV1.QWEN3_VL.value,
                "status": "COMPLETED",
                "target_host": PilotHostV1.MAI_UI.value,
            }
        )
        return AdapterStageResultV1(
            stage=RunStageV1.MAI_LIVE_SMOKE,
            manifest_sha256=context.manifest_sha256,
            evidence_sha256=hashlib.sha256(evidence).hexdigest(),
            evidence_preimage=evidence,
            actor_calls=0,
            openai_calls=0,
            actor_actions=0,
            cost_usd_micros=0,
            completed_units=("resource-handoff:QWEN3_VL:MAI_UI",),
            provider_final_request_proven=False,
        )

    def cleanup_success_evidence_preimage(self) -> bytes | None:
        return self._cleanup_success_evidence

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None:
        return self._failure_evidence or _cpu_failure_evidence(stage, "RESOURCE_ADAPTER_FAILED")


class _CpuSmokeAdapterV1:
    def __init__(self, fault: CpuTestFaultV1) -> None:
        self._fault = fault

    def run_host(
        self,
        host: PilotHostV1,
        plan: HostLiveSmokePlanV1,
        actor_resource: SnapshotResourceV1,
        openai_stages: tuple[OpenAIResponsesStageV1, ...],
        context: StageAdapterContextV1,
        lease: CaseAuthorityBrokerV1,
    ) -> AdapterStageResultV1:
        del lease, openai_stages
        stage = (
            RunStageV1.QWEN_LIVE_SMOKE
            if host is PilotHostV1.QWEN3_VL
            else RunStageV1.MAI_LIVE_SMOKE
        )
        if (
            self._fault
            in {
                CpuTestFaultV1.QWEN_SMOKE_FAILURE,
                CpuTestFaultV1.QWEN_SMOKE_AND_RESOURCE_CLEANUP_FAILURE,
            }
            and stage is RunStageV1.QWEN_LIVE_SMOKE
        ):
            raise RuntimeError("adapter-private-detail-must-not-escape")
        if self._fault is CpuTestFaultV1.MAI_SMOKE_FAILURE and (stage is RunStageV1.MAI_LIVE_SMOKE):
            raise RuntimeError("adapter-private-detail-must-not-escape")
        if plan.host is not host or actor_resource.host is not host:
            raise RuntimeError("CPU smoke binding differs")
        evidence = _cpu_evidence(context.manifest_sha256, stage)
        return AdapterStageResultV1(
            stage=stage,
            manifest_sha256=context.manifest_sha256,
            evidence_sha256=hashlib.sha256(evidence).hexdigest(),
            evidence_preimage=evidence,
            actor_calls=sum(case.max_actor_calls for case in plan.cases),
            openai_calls=sum(case.max_openai_calls for case in plan.cases),
            actor_actions=0,
            cost_usd_micros=0,
            completed_units=tuple(f"{host.value}:{case.mode.value}" for case in plan.cases),
            provider_final_request_proven=True,
        )

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None:
        return _cpu_failure_evidence(stage, "STAGE_ADAPTER_FAILED")


class _CpuPilotAdapterV1:
    def __init__(self, fault: CpuTestFaultV1) -> None:
        self._fault = fault

    def run_pilot(
        self,
        pilot: FrozenPilotManifestV1,
        actor_resources: tuple[SnapshotResourceV1, ...],
        openai_stages: tuple[OpenAIResponsesStageV1, ...],
        context: StageAdapterContextV1,
        lease: CaseAuthorityBrokerV1,
    ) -> AdapterStageResultV1:
        del lease, actor_resources, openai_stages
        actor_calls = len(pilot.cells)
        if self._fault is CpuTestFaultV1.PILOT_ACTOR_BUDGET_OVERRUN:
            actor_calls = pilot.max_total_actor_calls + 1
        evidence = _cpu_evidence(context.manifest_sha256, RunStageV1.R25_PILOT)
        return AdapterStageResultV1(
            stage=RunStageV1.R25_PILOT,
            manifest_sha256=context.manifest_sha256,
            evidence_sha256=hashlib.sha256(evidence).hexdigest(),
            evidence_preimage=evidence,
            actor_calls=actor_calls,
            openai_calls=0,
            actor_actions=len(pilot.cells),
            cost_usd_micros=0,
            completed_units=tuple(f"pilot-cell-{index:03d}" for index, _ in enumerate(pilot.cells)),
            provider_final_request_proven=True,
        )

    def failure_evidence_preimage(self, stage: RunStageV1) -> bytes | None:
        return _cpu_failure_evidence(stage, "STAGE_ADAPTER_FAILED")


class CpuTestR24R25ExecutorV1(_SequenceExecutorCoreV1):
    """Exact sealed CPU executor; accepts no callbacks, commands, or secret value."""

    def __init__(
        self,
        manifest: R24R25RunAuthorityManifestV1,
        *,
        confirmed_manifest_sha256: str,
        repository_root: Path,
        fault: CpuTestFaultV1,
        seal: object,
    ) -> None:
        if seal is not _MODULE_SEAL or type(fault) is not CpuTestFaultV1:
            raise ValueError("CPU executor must be created by its module-owned factory")
        if manifest.schema_version != R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V2:
            raise ValueError("CPU full executor requires exact v2 authority")
        assert manifest.runtime_config_sha256 is not None
        assert manifest.resource_topology is not None
        assert manifest.max_resource_cleanup_wall_time_seconds is not None
        bundle = _AdapterBundleV1(
            seal=_MODULE_SEAL,
            resources=_CpuResourceAdapterV1(fault),
            smoke=_CpuSmokeAdapterV1(fault),
            pilot=_CpuPilotAdapterV1(fault),
            secret_leases=_CpuSecretLeaseProviderV1(fault),
            pilot_switch_authority=None,
            pilot_switch_authority_sha256=None,
            production=False,
        )
        cleanup_upper_bound_preimage = _canonical_bytes(
            {
                "domain": "cpu-test-full-resource-cleanup-bound",
                "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
                "value": {
                    "cleanup_upper_bound_seconds": (
                        manifest.max_resource_cleanup_wall_time_seconds
                    ),
                    "resource_topology": manifest.resource_topology,
                    "runtime_config_sha256": manifest.runtime_config_sha256,
                },
            }
        )
        super().__init__(
            manifest,
            confirmed_manifest_sha256=confirmed_manifest_sha256,
            preflight_report_sha256=hashlib.sha256(
                f"CPU_FULL_PREFLIGHT\0{confirmed_manifest_sha256}".encode()
            ).hexdigest(),
            factory_binding_sha256=hashlib.sha256(
                f"CPU_FULL_FACTORY\0{confirmed_manifest_sha256}".encode()
            ).hexdigest(),
            confirmed_runtime_config_sha256=manifest.runtime_config_sha256,
            resource_cleanup_upper_bound_seconds=(manifest.max_resource_cleanup_wall_time_seconds),
            resource_cleanup_upper_bound_preimage=cleanup_upper_bound_preimage,
            resource_cleanup_upper_bound_sha256=hashlib.sha256(
                cleanup_upper_bound_preimage
            ).hexdigest(),
            repository_root=repository_root,
            adapters=bundle,
        )


class CpuTestR24SmokeExecutorV1(_R24SmokeExecutorCoreV1):
    """CPU-only smoke executor with no pilot adapter or executable callback."""

    def __init__(
        self,
        manifest: R24SmokeRunAuthorityManifestV1,
        *,
        confirmed_manifest_sha256: str,
        repository_root: Path,
        fault: CpuTestFaultV1,
        seal: object,
    ) -> None:
        if seal is not _MODULE_SEAL or type(fault) is not CpuTestFaultV1:
            raise ValueError("CPU smoke executor must be module-built")
        preflight_report_sha256 = hashlib.sha256(
            f"CPU_SMOKE_PREFLIGHT\0{confirmed_manifest_sha256}".encode()
        ).hexdigest()
        factory_binding_sha256 = hashlib.sha256(
            f"CPU_SMOKE_FACTORY\0{confirmed_manifest_sha256}".encode()
        ).hexdigest()
        cleanup_upper_bound_preimage = _canonical_bytes(
            {
                "domain": "cpu-test-resource-cleanup-bound",
                "schema_version": (
                    "mobileworld.runtime.sentinel-r2.4-cpu-test-resource-cleanup-bound/v1"
                ),
                "value": {
                    "cleanup_upper_bound_seconds": (R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS),
                    "manifest_sha256": confirmed_manifest_sha256,
                },
            }
        )
        bundle = _R24SmokeAdapterBundleV1(
            seal=_MODULE_SEAL,
            resources=_CpuResourceAdapterV1(fault),
            smoke=_CpuSmokeAdapterV1(fault),
            secret_leases=_CpuSecretLeaseProviderV1(fault),
            production=False,
        )
        super().__init__(
            manifest,
            confirmed_manifest_sha256=confirmed_manifest_sha256,
            preflight_report_sha256=preflight_report_sha256,
            factory_binding_sha256=factory_binding_sha256,
            confirmed_runtime_config_sha256=manifest.runtime_config_sha256,
            resource_cleanup_upper_bound_seconds=(R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS),
            resource_cleanup_upper_bound_preimage=cleanup_upper_bound_preimage,
            resource_cleanup_upper_bound_sha256=hashlib.sha256(
                cleanup_upper_bound_preimage
            ).hexdigest(),
            repository_root=repository_root,
            adapters=bundle,
        )


def build_cpu_test_executor_v1(
    manifest: R24R25RunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
    repository_root: Path,
    fault: CpuTestFaultV1 = CpuTestFaultV1.NONE,
) -> CpuTestR24R25ExecutorV1:
    """Build a deterministic executor that performs no external-resource operation."""

    return CpuTestR24R25ExecutorV1(
        manifest,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
        repository_root=repository_root,
        fault=fault,
        seal=_MODULE_SEAL,
    )


def build_cpu_test_r24_smoke_executor_v1(
    manifest: R24SmokeRunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
    repository_root: Path,
    fault: CpuTestFaultV1 = CpuTestFaultV1.NONE,
) -> CpuTestR24SmokeExecutorV1:
    """Build the exact CPU/fake R2.4-only executor; it owns no pilot port."""

    return CpuTestR24SmokeExecutorV1(
        manifest,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
        repository_root=repository_root,
        fault=fault,
        seal=_MODULE_SEAL,
    )


def build_production_executor_v1(
    manifest: R24R25RunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
    factory: object,
    confirmed_runtime_config_sha256: str,
    repository_root: Path,
    resource_adapter: object,
    driver_adapters: object,
    case_authority_broker_provider: object,
) -> ProductionR24R25ExecutorV1:
    """Assemble production execution from exact checked-in implementations.

    Local imports avoid a module cycle while exact-type checks prevent callers
    from supplying structural protocols, commands, clients, or callbacks.
    """

    from mobile_world.runtime.sentinel.r2_4.production_driver import (
        PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V2,
        ProductionCaseAuthorityBrokerProviderV1,
        ProductionDriverAdaptersV1,
        ProductionResourceLifecycleAdapterV1,
        ProductionResourceTopologyV1,
        build_production_pilot_switch_authority_v1,
        production_pilot_switch_authority_sha256,
    )
    from mobile_world.runtime.sentinel.r2_4.production_preflight import (
        ProductionPostPreflightFactoryV1,
    )

    if type(resource_adapter) is not ProductionResourceLifecycleAdapterV1:
        raise LiveRunContractError(
            "PRODUCTION_RESOURCE_ADAPTER_REQUIRED",
            "exact production resource adapter is required",
        )
    if (
        type(factory) is not ProductionPostPreflightFactoryV1
        or factory.manifest_sha256 != confirmed_manifest_sha256
        or factory.runtime_config_sha256 != confirmed_runtime_config_sha256
        or factory.sequence_execution_scope is not SequenceExecutionScopeV1.R24_R25_FULL
    ):
        raise LiveRunContractError(
            "POST_PREFLIGHT_FACTORY_BINDING_MISMATCH",
            "full executor requires the exact runtime-bound post-preflight factory",
        )
    if type(driver_adapters) is not ProductionDriverAdaptersV1:
        raise LiveRunContractError(
            "PRODUCTION_DRIVER_ADAPTER_REQUIRED",
            "exact production driver adapters are required",
        )
    if driver_adapters.resource_lifecycle is not resource_adapter:
        raise LiveRunContractError(
            "RESOURCE_LIFECYCLE_BINDING_MISMATCH",
            "executor and production driver must share one lifecycle authority",
        )
    if type(case_authority_broker_provider) is not ProductionCaseAuthorityBrokerProviderV1:
        raise LiveRunContractError(
            "CASE_AUTHORITY_BROKER_REQUIRED",
            "exact post-preflight case authority broker is required",
        )
    cleanup_seconds, cleanup_preimage, cleanup_sha256 = _production_cleanup_bound_seconds(
        resource_adapter,
        expected_schema_version=PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V2,
        runtime_config_sha256=confirmed_runtime_config_sha256,
        full_bound=True,
    )
    if (
        cleanup_seconds != manifest.max_resource_cleanup_wall_time_seconds
        or cleanup_sha256 != manifest.resource_cleanup_upper_bound_sha256
        or resource_adapter.resource_topology.value != manifest.resource_topology
    ):
        raise LiveRunContractError(
            "RESOURCE_CLEANUP_BOUND_MISMATCH",
            "executor resource proof differs from owner manifest",
        )
    pilot_switch_authority: object | None = None
    pilot_switch_authority_sha256: str | None = None
    if (
        resource_adapter.resource_topology
        is ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
    ):
        pilot_switch_authority = build_production_pilot_switch_authority_v1(
            factory=factory,
            resource_lifecycle=resource_adapter,
            confirmed_runtime_config_sha256=confirmed_runtime_config_sha256,
            confirmed_cleanup_upper_bound_sha256=cleanup_sha256,
        )
        pilot_switch_authority_sha256 = production_pilot_switch_authority_sha256(
            pilot_switch_authority
        )
    bundle = _AdapterBundleV1(
        seal=_MODULE_SEAL,
        resources=resource_adapter,
        smoke=driver_adapters.smoke,
        pilot=driver_adapters.pilot,
        secret_leases=case_authority_broker_provider,
        pilot_switch_authority=pilot_switch_authority,
        pilot_switch_authority_sha256=pilot_switch_authority_sha256,
        production=True,
    )
    return ProductionR24R25ExecutorV1(
        manifest,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
        preflight_report_sha256=factory.preflight_report_sha256,
        factory_binding_sha256=factory.factory_binding_sha256,
        confirmed_runtime_config_sha256=confirmed_runtime_config_sha256,
        resource_cleanup_upper_bound_seconds=cleanup_seconds,
        resource_cleanup_upper_bound_preimage=cleanup_preimage,
        resource_cleanup_upper_bound_sha256=cleanup_sha256,
        repository_root=repository_root,
        module_owned_adapters=bundle,
        seal=_PRODUCTION_EXECUTOR_SEAL,
    )


def _production_cleanup_bound_seconds(
    resource_adapter: object,
    *,
    expected_schema_version: str,
    runtime_config_sha256: str,
    full_bound: bool = False,
) -> tuple[int, bytes, str]:
    try:
        prefix = "full_cleanup" if full_bound else "cleanup"
        upper_bound = getattr(resource_adapter, f"{prefix}_upper_bound_seconds")
        preimage = getattr(resource_adapter, f"{prefix}_upper_bound_preimage")
        confirmed_sha256 = getattr(resource_adapter, f"{prefix}_upper_bound_sha256")
    except Exception as exc:
        raise LiveRunContractError(
            "RESOURCE_CLEANUP_BOUND_MISMATCH",
            "shared resource cleanup bound is unavailable",
        ) from exc
    if (
        type(upper_bound) is not int
        or upper_bound < 1
        or type(preimage) is not bytes
        or not preimage
        or len(preimage) > 64 * 1024
        or not _is_lower_hex(confirmed_sha256, 64)
        or hashlib.sha256(preimage).hexdigest() != confirmed_sha256
    ):
        raise LiveRunContractError(
            "RESOURCE_CLEANUP_BOUND_MISMATCH",
            "shared resource cleanup bound proof differs",
        )
    try:
        envelope = json.loads(preimage.decode("utf-8"))
    except Exception as exc:
        raise LiveRunContractError(
            "RESOURCE_CLEANUP_BOUND_MISMATCH",
            "shared resource cleanup bound proof is invalid",
        ) from exc
    if (
        type(envelope) is not dict
        or _canonical_bytes(envelope) != preimage
        or set(envelope) != {"domain", "schema_version", "value"}
        or envelope.get("domain") != "production-resource-cleanup-bound"
        or envelope.get("schema_version") != expected_schema_version
        or type(envelope.get("value")) is not dict
    ):
        raise LiveRunContractError(
            "RESOURCE_CLEANUP_BOUND_MISMATCH",
            "shared resource cleanup bound envelope differs",
        )
    projection = cast(dict[str, object], envelope["value"])
    expected_keys = {
        "admitted_backend_cleanup_upper_bound_seconds",
        "admitted_model_cleanup_upper_bound_seconds",
        "backend_cleanup_upper_bound_seconds",
        "cleanup_upper_bound_seconds",
        "docker_command_timeout_seconds",
        "final_shared_gpu_attestation_command_slots",
        "final_shared_gpu_attestation_upper_bound_seconds",
        "health_poll_interval_ceiling_seconds",
        "health_poll_interval_ms",
        "model_cleanup_upper_bound_seconds",
        "model_leader_wait_slots",
        "model_poll_overshoot_slots",
        "model_port_wait_slots",
        "model_session_wait_slots",
        "nvidia_attestation_command_timeout_seconds",
        "partial_model_cleanup_upper_bound_seconds",
        "pending_backend_cleanup_command_slots",
        "pending_backend_cleanup_upper_bound_seconds",
        "resource_topology",
        "runtime_config_sha256",
        "shutdown_grace_seconds",
    }
    if full_bound:
        expected_keys.add("maximum_model_cleanup_count")
    integer_keys = expected_keys - {"resource_topology", "runtime_config_sha256"}
    if set(projection) != expected_keys or any(
        type(projection.get(key)) is not int for key in integer_keys
    ):
        raise LiveRunContractError(
            "RESOURCE_CLEANUP_BOUND_MISMATCH",
            "shared resource cleanup bound projection differs",
        )
    shutdown = cast(int, projection["shutdown_grace_seconds"])
    poll_ms = cast(int, projection["health_poll_interval_ms"])
    poll_ceiling = cast(int, projection["health_poll_interval_ceiling_seconds"])
    docker_timeout = cast(int, projection["docker_command_timeout_seconds"])
    nvidia_timeout = cast(int, projection["nvidia_attestation_command_timeout_seconds"])
    admitted_model = 5 * shutdown + 3 * poll_ceiling
    partial_model = 3 * shutdown + 2 * poll_ceiling
    model = max(admitted_model, partial_model)
    admitted_backend = max(3 * docker_timeout, shutdown + 2 * docker_timeout)
    pending_backend = 7 * docker_timeout
    backend = max(admitted_backend, pending_backend)
    topology = projection["resource_topology"]
    maximum_model_cleanup_count = (
        (
            1
            if topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
            else 2
            if topology == "INDEPENDENT_GPU_CONCURRENT"
            else 0
        )
        if full_bound
        else (1 if topology == "SINGLE_GPU_SEQUENTIAL_SHARED" else 0)
    )
    final_attestation_slots = 1 if topology == "SINGLE_GPU_SEQUENTIAL_SHARED" else 0
    final_attestation = final_attestation_slots * nvidia_timeout
    recomputed = maximum_model_cleanup_count * model + backend + final_attestation
    if (
        shutdown < 1
        or poll_ms < 1
        or poll_ceiling != (poll_ms + 999) // 1_000
        or projection["model_leader_wait_slots"] != 2
        or projection["model_poll_overshoot_slots"] != 3
        or projection["model_port_wait_slots"] != 1
        or projection["model_session_wait_slots"] != 2
        or projection["pending_backend_cleanup_command_slots"] != 7
        or maximum_model_cleanup_count == 0
        or (full_bound and projection["maximum_model_cleanup_count"] != maximum_model_cleanup_count)
        or projection["final_shared_gpu_attestation_command_slots"] != final_attestation_slots
        or projection["admitted_model_cleanup_upper_bound_seconds"] != admitted_model
        or projection["partial_model_cleanup_upper_bound_seconds"] != partial_model
        or projection["model_cleanup_upper_bound_seconds"] != model
        or projection["admitted_backend_cleanup_upper_bound_seconds"] != admitted_backend
        or projection["pending_backend_cleanup_upper_bound_seconds"] != pending_backend
        or projection["backend_cleanup_upper_bound_seconds"] != backend
        or projection["final_shared_gpu_attestation_upper_bound_seconds"] != final_attestation
        or projection["cleanup_upper_bound_seconds"] != recomputed
        or recomputed != upper_bound
        or projection["runtime_config_sha256"] != runtime_config_sha256
    ):
        raise LiveRunContractError(
            "RESOURCE_CLEANUP_BOUND_MISMATCH",
            "shared resource cleanup bound cannot be independently recomputed",
        )
    return upper_bound, preimage, confirmed_sha256


def build_production_r24_smoke_executor_v1(
    manifest: R24SmokeRunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
    confirmed_runtime_config_sha256: str,
    repository_root: Path,
    post_preflight_factory: object,
    resource_adapter: object,
    driver_adapters: object,
    case_authority_broker_provider: object,
) -> ProductionR24SmokeExecutorV1:
    """Assemble the smoke-only production executor without retaining a pilot port."""

    from mobile_world.runtime.sentinel.r2_4.production_driver import (
        PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1,
        ProductionCaseAuthorityBrokerProviderV1,
        ProductionDriverAdaptersV1,
        ProductionResourceLifecycleAdapterV1,
    )
    from mobile_world.runtime.sentinel.r2_4.production_preflight import (
        ProductionPostPreflightFactoryV1,
    )

    if type(manifest) is not R24SmokeRunAuthorityManifestV1:
        raise LiveRunContractError(
            "R24_SMOKE_AUTHORITY_REQUIRED", "exact smoke-only authority is required"
        )
    if type(post_preflight_factory) is not ProductionPostPreflightFactoryV1:
        raise LiveRunContractError(
            "POST_PREFLIGHT_FACTORY_REQUIRED", "exact smoke post-preflight factory is required"
        )
    factory_manifest = post_preflight_factory.manifest_snapshot()
    if (
        post_preflight_factory.sequence_execution_scope
        is not SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY
        or post_preflight_factory.manifest_sha256 != confirmed_manifest_sha256
        or post_preflight_factory.runtime_config_sha256 != confirmed_runtime_config_sha256
        or type(factory_manifest) is not R24SmokeRunAuthorityManifestV1
        or smoke_authority_manifest_sha256(factory_manifest) != confirmed_manifest_sha256
    ):
        raise LiveRunContractError(
            "SMOKE_FACTORY_BINDING_MISMATCH", "smoke factory authority differs"
        )
    if type(resource_adapter) is not ProductionResourceLifecycleAdapterV1 or (
        resource_adapter.runtime_config_sha256 != confirmed_runtime_config_sha256
    ):
        raise LiveRunContractError(
            "PRODUCTION_RESOURCE_ADAPTER_REQUIRED",
            "exact runtime-bound shared resource adapter is required",
        )
    if type(driver_adapters) is not ProductionDriverAdaptersV1 or (
        driver_adapters.resource_lifecycle is not resource_adapter
    ):
        raise LiveRunContractError(
            "PRODUCTION_DRIVER_ADAPTER_REQUIRED", "smoke driver/resource binding differs"
        )
    if type(case_authority_broker_provider) is not ProductionCaseAuthorityBrokerProviderV1:
        raise LiveRunContractError(
            "CASE_AUTHORITY_BROKER_REQUIRED", "exact smoke case broker provider is required"
        )
    driver_factory = getattr(getattr(driver_adapters, "_port", None), "_factory", None)
    broker_factory = getattr(case_authority_broker_provider, "_factory", None)
    if driver_factory is not post_preflight_factory or broker_factory is not post_preflight_factory:
        raise LiveRunContractError(
            "SMOKE_FACTORY_COMPONENT_BINDING_MISMATCH",
            "driver and case broker must share the exact smoke post-preflight factory",
        )
    (
        cleanup_upper_bound_seconds,
        cleanup_upper_bound_preimage,
        cleanup_upper_bound_sha256,
    ) = _production_cleanup_bound_seconds(
        resource_adapter,
        expected_schema_version=PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1,
        runtime_config_sha256=confirmed_runtime_config_sha256,
    )
    if manifest.max_resource_cleanup_wall_time_seconds < max(
        R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS,
        cleanup_upper_bound_seconds,
    ):
        raise LiveRunContractError(
            "INSUFFICIENT_RESOURCE_CLEANUP_RESERVE",
            "owner cleanup reserve is below the sealed resource cleanup upper bound",
        )
    bundle = _R24SmokeAdapterBundleV1(
        seal=_MODULE_SEAL,
        resources=resource_adapter,
        smoke=driver_adapters.smoke,
        secret_leases=case_authority_broker_provider,
        production=True,
    )
    return ProductionR24SmokeExecutorV1(
        manifest,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
        preflight_report_sha256=post_preflight_factory.preflight_report_sha256,
        factory_binding_sha256=post_preflight_factory.factory_binding_sha256,
        confirmed_runtime_config_sha256=confirmed_runtime_config_sha256,
        resource_cleanup_upper_bound_seconds=cleanup_upper_bound_seconds,
        resource_cleanup_upper_bound_preimage=cleanup_upper_bound_preimage,
        resource_cleanup_upper_bound_sha256=cleanup_upper_bound_sha256,
        repository_root=repository_root,
        module_owned_adapters=bundle,
        seal=_PRODUCTION_EXECUTOR_SEAL,
    )


PRODUCTION_ROOT_HOOKS_V1: Final[tuple[str, ...]] = (
    "CaseAuthorityBrokerProviderPortV1: post-preflight request-bound leases; no stage API key",
    "ResourceLifecycleAdapterPortV1: verified Qwen/MAI snapshots, GPU services, Docker/backend",
    "LiveSmokeAdapterPortV1: fixed Qwen/MAI OFF-SHADOW-ACTIVE request/proof runner",
    "PilotAdapterPortV1: frozen isolated MobileWorld cells, resets, actions, official metric",
    "module-owned metering: actor/OpenAI/action/cost/time census and kill switches",
)


def production_executor_available_v1() -> bool:
    """The exact dependency-injected production executor is checked in."""

    return True


def _cpu_protocol_assertion(value: CpuTestR24R25ExecutorV1) -> SequenceStageExecutorV1:
    return value


def _production_protocol_assertion(value: ProductionR24R25ExecutorV1) -> SequenceStageExecutorV1:
    return value


__all__ = [
    "LIVE_EXECUTOR_BINDING_SCHEMA_VERSION",
    "PRODUCTION_ROOT_HOOKS_V1",
    "R24_SMOKE_EXECUTOR_BINDING_SCHEMA_VERSION",
    "R24_SMOKE_MAI_FAILURE_EVIDENCE_SCHEMA_VERSION",
    "R24_SMOKE_MAI_TERMINAL_EVIDENCE_SCHEMA_VERSION",
    "AdapterStageResultV1",
    "AtomicExternalOutputTransactionV1",
    "AtomicR24SmokeOutputTransactionV1",
    "CaseAuthorityBrokerV1",
    "CaseAuthorityBrokerProviderPortV1",
    "CaseExecutionLeaseBindingV1",
    "CpuTestFaultV1",
    "CpuTestR24R25ExecutorV1",
    "CpuTestR24SmokeExecutorV1",
    "ExecutorCensusV1",
    "ExecutorStateV1",
    "LiveSmokeAdapterPortV1",
    "PilotAdapterPortV1",
    "ProductionR24R25ExecutorV1",
    "ProductionR24SmokeExecutorV1",
    "R24SmokeSequenceResultV1",
    "ResourceLifecycleAdapterPortV1",
    "SecureSecretLeaseProviderPortV1",
    "SecureSecretLeaseV1",
    "StageAdapterContextV1",
    "UnavailableSecureSecretLeaseProviderV1",
    "build_cpu_test_executor_v1",
    "build_cpu_test_r24_smoke_executor_v1",
    "build_production_executor_v1",
    "build_production_r24_smoke_executor_v1",
    "production_executor_available_v1",
    "r24_smoke_sequence_result_projection",
    "r24_smoke_sequence_result_sha256",
]
