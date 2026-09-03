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
from typing import Final, Protocol, runtime_checkable

from mobile_world.runtime.sentinel.r2_4.live_run import (
    HostLiveSmokePlanV1,
    LiveRunContractError,
    OpenAIResponsesStageV1,
    R24R25RunAuthorityManifestV1,
    RunStageV1,
    SecretFileReferenceV1,
    SequenceStageExecutorV1,
    SmokeModeV1,
    SnapshotResourceV1,
    StageExecutionReceiptV1,
    authority_manifest_projection,
    authority_manifest_sha256,
    parse_authority_manifest,
)
from mobile_world.runtime.sentinel.r2_5.pilot import FrozenPilotManifestV1, PilotHostV1

LIVE_EXECUTOR_BINDING_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-r2.5-executor-binding/v1"


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
    run_id: str
    source_commit: str
    remaining_actor_calls: int
    remaining_openai_calls: int
    remaining_cost_usd_micros: int
    remaining_wall_time_ms: int
    authority_deadline_monotonic_ns: int

    def __post_init__(self) -> None:
        if not _is_lower_hex(self.manifest_sha256, 64) or not _is_lower_hex(self.source_commit, 40):
            raise ValueError("stage context hashes are invalid")
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


class AtomicExternalOutputTransactionV1:
    """One all-or-nothing, repository-external sequence output transaction."""

    _STAGE_FILES: Final[dict[RunStageV1, str]] = {
        RunStageV1.RESOURCE_PREFLIGHT: "00-resource-preflight.json",
        RunStageV1.QWEN_LIVE_SMOKE: "01-qwen-live-smoke.json",
        RunStageV1.MAI_LIVE_SMOKE: "02-mai-live-smoke.json",
        RunStageV1.R25_PILOT: "03-r25-pilot.json",
    }

    def __init__(
        self,
        *,
        output_root: Path,
        repository_root: Path,
        run_id: str,
        source_commit: str,
        manifest_sha256: str,
    ) -> None:
        if not _is_lower_hex(manifest_sha256, 64) or not _is_lower_hex(source_commit, 40):
            raise ValueError("transaction hashes are invalid")
        if type(run_id) is not str or not run_id:
            raise ValueError("transaction run_id is invalid")
        repository = repository_root.resolve(strict=True)
        if not repository.is_dir():
            raise ValueError("repository_root must be a directory")
        parent = output_root.parent.resolve(strict=True)
        resolved_output = output_root.resolve(strict=False)
        if (
            output_root.exists()
            or output_root.is_symlink()
            or _is_within(resolved_output, repository)
        ):
            raise ValueError("output_root must be fresh and repository-external")
        if not parent.is_dir():
            raise ValueError("output_root parent must be a directory")
        self._output_root = resolved_output
        self._parent = parent
        self._staging = parent / f".{output_root.name}.{manifest_sha256[:16]}.partial"
        if self._staging.exists() or self._staging.is_symlink():
            raise ValueError("stale output transaction exists")
        self._binding = {
            "manifest_sha256": manifest_sha256,
            "run_id": run_id,
            "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
            "source_commit": source_commit,
        }
        self._begun = False
        self._committed = False
        self._failed = False
        self._recorded: list[RunStageV1] = []

    @property
    def committed(self) -> bool:
        return self._committed

    @property
    def output_root(self) -> Path:
        return self._output_root

    def _write_once(self, name: str, payload: bytes) -> None:
        target = self._staging / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        self._fsync_directory(self._staging)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("output transaction directory changed type")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def begin(self) -> None:
        if self._committed:
            raise RuntimeError("output transaction is already committed")
        if self._begun:
            return
        os.mkdir(self._staging, mode=0o700)
        os.chmod(self._staging, 0o700)
        self._fsync_directory(self._staging)
        self._fsync_directory(self._parent)
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
            "evidence": evidence,
            "receipt": _receipt_projection(receipt),
            "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
        }
        self._write_once(self._STAGE_FILES[receipt.stage], _canonical_bytes(payload))
        self._recorded.append(receipt.stage)

    def fail(
        self,
        *,
        failed_stage: RunStageV1,
        failure_code: str,
        stage_failure_evidence_preimage: bytes | None,
        resource_cleanup_evidence_preimage: bytes | None = None,
        resource_cleanup_failure_code: str | None = None,
        resource_cleanup_status: str = "SUCCEEDED",
    ) -> None:
        """Atomically publish an incomplete run as failure evidence, never success."""

        if self._committed or self._failed or self._output_root.exists():
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
        if resource_cleanup_status not in {"SUCCEEDED", "RETRY_REQUIRED"} or (
            resource_cleanup_status == "RETRY_REQUIRED"
            and (resource_cleanup_failure_code is None or resource_cleanup_evidence is None)
        ):
            raise RuntimeError("resource cleanup status is invalid")
        self._write_once(
            "failure.json",
            _canonical_bytes(
                {
                    **self._binding,
                    "completed_stages": [item.value for item in self._recorded],
                    "failed_stage": failed_stage.value,
                    "failure_code": failure_code,
                    "resource_cleanup_evidence": resource_cleanup_evidence,
                    "resource_cleanup_evidence_sha256": resource_cleanup_evidence_sha256,
                    "resource_cleanup_failure_code": resource_cleanup_failure_code,
                    "resource_cleanup_status": resource_cleanup_status,
                    "stage_failure_evidence": stage_failure_evidence,
                    "stage_failure_evidence_sha256": stage_failure_evidence_sha256,
                    "status": "FAILED",
                }
            ),
        )
        self._fsync_directory(self._staging)
        os.replace(self._staging, self._output_root)
        self._fsync_directory(self._parent)
        self._failed = True

    def preserve_failure_recovery(
        self,
        *,
        failed_stage: RunStageV1,
        failure_code: str,
    ) -> None:
        """Durably mark an unpublishable failure without deleting prior evidence."""

        root: Path | None = None
        for candidate in (self._staging, self._output_root):
            if candidate.is_dir() and not candidate.is_symlink():
                root = candidate
                break
        if root is None:
            raise RuntimeError("failure recovery directory is unavailable")
        marker = root / "recovery.json"
        payload = _canonical_bytes(
            {
                **self._binding,
                "completed_stages": [item.value for item in self._recorded],
                "failed_stage": failed_stage.value,
                "failure_code": failure_code,
                "status": "FAILURE_PUBLICATION_INCOMPLETE",
            }
        )
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or marker.read_bytes() != payload:
                raise RuntimeError("failure recovery marker differs")
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(marker, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
        self._fsync_directory(root)
        self._fsync_directory(self._parent)

    def commit(self) -> None:
        if (
            not self._begun
            or self._committed
            or self._failed
            or tuple(self._recorded) != tuple(self._STAGE_FILES)
            or self._output_root.exists()
            or self._output_root.is_symlink()
        ):
            raise RuntimeError("output transaction is incomplete or no longer fresh")
        self._fsync_directory(self._staging)
        os.replace(self._staging, self._output_root)
        self._fsync_directory(self._parent)
        self._committed = True

    def rollback(self) -> None:
        if self._committed or self._failed:
            return
        if self._staging.parent != self._parent:
            raise RuntimeError("output staging path escaped its bound parent")
        if self._staging.is_symlink():
            self._staging.unlink()
        elif self._staging.exists():
            if not self._staging.is_dir():
                raise RuntimeError("output staging target changed type")
            shutil.rmtree(self._staging)
        self._fsync_directory(self._parent)
        self._begun = False
        self._recorded.clear()


class _AdapterBundleV1:
    """Identity-sealed bundle; only this module constructs instances."""

    __slots__ = ("pilot", "production", "resources", "secret_leases", "smoke")

    def __init__(
        self,
        *,
        seal: object,
        resources: ResourceLifecycleAdapterPortV1,
        smoke: LiveSmokeAdapterPortV1,
        pilot: PilotAdapterPortV1,
        secret_leases: SecureSecretLeaseProviderPortV1,
        production: bool,
    ) -> None:
        if seal is not _MODULE_SEAL:
            raise ValueError("adapter bundle is not module-owned")
        self.resources = resources
        self.smoke = smoke
        self.pilot = pilot
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
        repository_root: Path,
        adapters: _AdapterBundleV1,
    ) -> None:
        if type(manifest) is not R24R25RunAuthorityManifestV1:
            raise ValueError("manifest must use the exact authority type")
        trusted_manifest = parse_authority_manifest(authority_manifest_projection(manifest))
        manifest_hash = authority_manifest_sha256(trusted_manifest)
        if confirmed_manifest_sha256 != manifest_hash:
            raise ValueError("confirmed manifest SHA-256 differs")
        if type(adapters) is not _AdapterBundleV1:
            raise ValueError("executor adapters are not module-owned")
        self._manifest_sha256 = manifest_hash
        self._manifest = trusted_manifest
        self._run_id = trusted_manifest.run_id
        self._source_commit = trusted_manifest.source_commit
        expires = datetime.strptime(
            trusted_manifest.authorization.expires_at_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
        remaining_authority_ns = int((expires - datetime.now(UTC)).total_seconds() * 1_000_000_000)
        self._authority_deadline_monotonic_ns = time.monotonic_ns() + max(0, remaining_authority_ns)
        self._adapters = adapters
        self._output = AtomicExternalOutputTransactionV1(
            output_root=Path(trusted_manifest.output_root),
            repository_root=repository_root,
            run_id=trusted_manifest.run_id,
            source_commit=trusted_manifest.source_commit,
            manifest_sha256=manifest_hash,
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
        self._lock = threading.Lock()

    def _context(self, manifest: R24R25RunAuthorityManifestV1) -> StageAdapterContextV1:
        return StageAdapterContextV1(
            manifest_sha256=self._manifest_sha256,
            run_id=self._run_id,
            source_commit=self._source_commit,
            remaining_actor_calls=manifest.max_sequence_actor_calls
            - sum(receipt.actor_calls for receipt in self._receipts),
            remaining_openai_calls=manifest.max_sequence_openai_calls
            - sum(receipt.openai_calls for receipt in self._receipts),
            remaining_cost_usd_micros=manifest.max_sequence_cost_usd_micros
            - sum(receipt.cost_usd_micros for receipt in self._receipts),
            remaining_wall_time_ms=manifest.max_sequence_wall_time_seconds * 1000
            - sum(receipt.wall_time_ms for receipt in self._receipts),
            authority_deadline_monotonic_ns=self._authority_deadline_monotonic_ns,
        )

    def _cleanup(self, context: StageAdapterContextV1) -> bool:
        if self._cleanup_succeeded:
            return True
        self._cleanup_attempted = True
        try:
            self._adapters.resources.cleanup(context)
        except Exception as exc:
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
        failure_publish_ok = True
        try:
            self._output.fail(
                failed_stage=failed_stage,
                failure_code=code,
                stage_failure_evidence_preimage=failure_evidence_preimage,
                resource_cleanup_evidence_preimage=(
                    None if cleanup_ok else self._resource_cleanup_failure_evidence
                ),
                resource_cleanup_failure_code=(
                    None if cleanup_ok else self._resource_cleanup_failure_code
                ),
                resource_cleanup_status=("SUCCEEDED" if cleanup_ok else "RETRY_REQUIRED"),
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
                "EXECUTOR_CLEANUP_FAILED", "executor cleanup did not complete"
            ) from None
        if not failure_publish_ok:
            raise LiveRunContractError(
                "EXECUTOR_FAILURE_PUBLICATION_FAILED",
                "failure evidence remains in the owner-only recovery directory",
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
                return self._adapters.resources.prepare(manifest.actor_resources, context)
            except Exception:
                raise _StageFailure(
                    "RESOURCE_ADAPTER_FAILED",
                    self._adapters.resources.failure_evidence_preimage(stage),
                ) from None
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
        adapter_failed = False
        failure_evidence: bytes | None = None
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
        except Exception:
            adapter_failed = True
            failed_adapter: object = (
                self._adapters.smoke
                if stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}
                else self._adapters.pilot
            )
            failure_evidence = failed_adapter.failure_evidence_preimage(stage)  # type: ignore[attr-defined]
        try:
            lease.close()
            self._secret_leases_closed += 1
        except Exception:
            raise _StageFailure("CASE_AUTHORITY_BROKER_CLOSE_FAILED") from None
        if adapter_failed:
            raise _StageFailure("STAGE_ADAPTER_FAILED", failure_evidence)
        if type(result) is not AdapterStageResultV1:
            raise _StageFailure("INVALID_ADAPTER_RESULT")
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
                <= sum(case.max_wall_time_seconds for case in plan.cases) * 1000
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
                and receipt.wall_time_ms <= manifest.pilot.max_total_wall_time_seconds * 1000
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
            context = self._context(trusted_manifest)
            current_stage_preimage: bytes | None = None
            if self._state in {ExecutorStateV1.COMPLETE, ExecutorStateV1.FAILED}:
                raise LiveRunContractError("EXECUTOR_STOPPED", "executor is terminal")
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
                self._state = ExecutorStateV1.RUNNING
                try:
                    self._output.begin()
                except Exception:
                    raise _StageFailure("OUTPUT_TRANSACTION_FAILED") from None
                started_ns = time.monotonic_ns()
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
                        raise _StageFailure("EXECUTOR_CLEANUP_FAILED")
                    try:
                        self._output.commit()
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
                wall_time_ms=sum(receipt.wall_time_ms for receipt in self._receipts),
                secret_leases_acquired=self._secret_leases_acquired,
                secret_leases_closed=self._secret_leases_closed,
                cleanup_attempted=self._cleanup_attempted,
                cleanup_succeeded=self._cleanup_succeeded,
                output_committed=self._output.committed,
            )


class ProductionR24R25ExecutorV1(_SequenceExecutorCoreV1):
    """Production stage executor created only by the exact dependency factory."""

    def __init__(
        self,
        manifest: R24R25RunAuthorityManifestV1,
        *,
        confirmed_manifest_sha256: str,
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
        self._closed = True


class _CpuSecretLeaseProviderV1:
    def __init__(self, fault: CpuTestFaultV1) -> None:
        self._fault = fault

    def acquire(
        self,
        reference: SecretFileReferenceV1,
        *,
        manifest_sha256: str,
    ) -> CaseAuthorityBrokerV1:
        if self._fault is CpuTestFaultV1.SECRET_LEASE_UNAVAILABLE:
            raise RuntimeError("CPU lease unavailable")
        return _CpuLeaseV1(manifest_sha256, reference.environment_key)


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
        bundle = _AdapterBundleV1(
            seal=_MODULE_SEAL,
            resources=_CpuResourceAdapterV1(fault),
            smoke=_CpuSmokeAdapterV1(fault),
            pilot=_CpuPilotAdapterV1(fault),
            secret_leases=_CpuSecretLeaseProviderV1(fault),
            production=False,
        )
        super().__init__(
            manifest,
            confirmed_manifest_sha256=confirmed_manifest_sha256,
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


def build_production_executor_v1(
    manifest: R24R25RunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
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
        ProductionCaseAuthorityBrokerProviderV1,
        ProductionDriverAdaptersV1,
        ProductionResourceLifecycleAdapterV1,
    )

    if type(resource_adapter) is not ProductionResourceLifecycleAdapterV1:
        raise LiveRunContractError(
            "PRODUCTION_RESOURCE_ADAPTER_REQUIRED",
            "exact production resource adapter is required",
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
    bundle = _AdapterBundleV1(
        seal=_MODULE_SEAL,
        resources=resource_adapter,
        smoke=driver_adapters.smoke,
        pilot=driver_adapters.pilot,
        secret_leases=case_authority_broker_provider,
        production=True,
    )
    return ProductionR24R25ExecutorV1(
        manifest,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
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
    "AdapterStageResultV1",
    "AtomicExternalOutputTransactionV1",
    "CaseAuthorityBrokerV1",
    "CaseAuthorityBrokerProviderPortV1",
    "CaseExecutionLeaseBindingV1",
    "CpuTestFaultV1",
    "CpuTestR24R25ExecutorV1",
    "ExecutorCensusV1",
    "ExecutorStateV1",
    "LiveSmokeAdapterPortV1",
    "PilotAdapterPortV1",
    "ProductionR24R25ExecutorV1",
    "ResourceLifecycleAdapterPortV1",
    "SecureSecretLeaseProviderPortV1",
    "SecureSecretLeaseV1",
    "StageAdapterContextV1",
    "UnavailableSecureSecretLeaseProviderV1",
    "build_cpu_test_executor_v1",
    "build_production_executor_v1",
    "production_executor_available_v1",
]
