"""Sealed, non-connecting production preflight for the R2.4/R2.5 run.

The public preflight reads declared non-secret fixtures and model snapshot
files, plus filesystem and Git metadata.  It never reads the declared OpenAI
secret's contents, connects to an endpoint, probes an accelerator, talks to
Docker, loads a model, starts a backend, or executes an actor action.

The exact post-preflight factory is reachable only from an owner-confirmed
manifest, a passing sealed report, and an operator-confirmed pricing hash.
Secret bytes remain child-process-only and are never read by preflight or the
actor process.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, cast
from urllib.parse import urlsplit

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_run import (
    HostLiveSmokePlanV1,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    R24R25RunAuthorityManifestV1,
    ResourcePreflightReportV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SmokeModeV1,
    SnapshotResourceV1,
    _git_state,
    _is_within,
    authority_manifest_projection,
    authority_manifest_sha256,
    inspect_local_resources,
    parse_authority_manifest,
    preflight_report_projection,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    R24SmokeRunAuthorityManifestV1,
    SequenceExecutionScopeV1,
    parse_smoke_authority_manifest,
    smoke_authority_manifest_projection,
    smoke_authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import PilotHostV1

PRODUCTION_PREFLIGHT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-production-preflight/v1"
R24_SMOKE_PRODUCTION_PREFLIGHT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-smoke-production-preflight/v1"
)
CASE_EXECUTION_LEASE_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-case-execution-lease/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_UTC_SECOND = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

_REPORT_SEAL: Final[object] = object()
_LEASE_SEAL: Final[object] = object()
_FACTORY_SEAL: Final[object] = object()
_CAPABILITY_CONSTRUCTION_SEAL: Final[object] = object()


class CaseExecutionScopeV1(StrEnum):
    """Closed authority scope for an exact owner-authorized live case."""

    OWNER_AUTHORIZED_LIVE = "OWNER_AUTHORIZED_LIVE"


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


def _require_timestamp(value: object, name: str) -> str:
    if type(value) is not str or _UTC_SECOND.fullmatch(value) is None:
        raise ValueError(f"{name} must be UTC to seconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"{name} must be a real UTC timestamp") from exc
    return value


def _timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _canonical_now(value: datetime | None) -> tuple[datetime, str]:
    candidate: object = datetime.now(UTC) if value is None else value
    if type(candidate) is not datetime or candidate.tzinfo is None:
        raise ValueError("preflight current time must be timezone-aware")
    current = candidate
    normalized = current.astimezone(UTC).replace(microsecond=0)
    return normalized, normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class ProductionPreflightCheckV1:
    """One secret-free check result in the exact preflight census."""

    check_id: str
    passed: bool
    verification: str

    def __post_init__(self) -> None:
        if type(self.check_id) is not str or _ID.fullmatch(self.check_id) is None:
            raise ValueError("preflight check ID is invalid")
        if type(self.passed) is not bool:
            raise ValueError("preflight check status must be bool")
        if self.verification not in {
            "CANONICAL_SHA256",
            "CONTENT_SHA256",
            "DECLARATION",
            "METADATA",
        }:
            raise ValueError("preflight check verification is invalid")


@dataclass(frozen=True, slots=True)
class _SecretMetadataBindingV1:
    """One metadata-only, held identity for the declared secret file."""

    descriptor: int
    path: Path
    resolved_path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _ContentFileBindingV1:
    """Metadata frozen before a non-secret file is opened for hashing."""

    path: Path
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    link_count: int
    byte_count: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _SnapshotFileBindingV1:
    relative_path: str
    content: _ContentFileBindingV1


@dataclass(frozen=True, slots=True)
class ProductionPreflightReportV1:
    """Identity-sealed proof that all CPU-verifiable gates were evaluated."""

    schema_version: str
    run_id: str
    manifest_sha256: str
    source_commit: str
    checked_at_utc: str
    authority_expires_at_utc: str
    base_preflight_sha256: str
    declared_snapshot_tree_sha256s: tuple[str, ...]
    actor_loopback_ports: tuple[int, ...]
    pilot_task_manifest_sha256: str
    smoke_fixture_sha256s: tuple[str, ...]
    checks: tuple[ProductionPreflightCheckV1, ...]
    all_checks_passed: bool
    eligible_for_post_preflight_factory: bool
    production_activation_available: bool
    secret_content_reads: int
    endpoint_connections: int
    gpu_operations: int
    docker_operations: int
    model_loads: int
    backend_operations: int
    actor_actions: int
    files_written: int
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _REPORT_SEAL:
            raise PermissionError("production preflight reports are module-owned")
        if self.schema_version != PRODUCTION_PREFLIGHT_SCHEMA_VERSION:
            raise ValueError("production preflight schema differs")
        if type(self.run_id) is not str or _ID.fullmatch(self.run_id) is None:
            raise ValueError("production preflight run ID is invalid")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if type(self.source_commit) is not str or _GIT_SHA1.fullmatch(self.source_commit) is None:
            raise ValueError("source_commit must be full lowercase SHA-1")
        _timestamp(_require_timestamp(self.checked_at_utc, "checked_at_utc"))
        _timestamp(_require_timestamp(self.authority_expires_at_utc, "authority_expires_at_utc"))
        _require_sha256(self.base_preflight_sha256, "base_preflight_sha256")
        if (
            type(self.declared_snapshot_tree_sha256s) is not tuple
            or len(self.declared_snapshot_tree_sha256s) != 2
        ):
            raise ValueError("preflight must bind exactly two snapshot tree hashes")
        for digest in self.declared_snapshot_tree_sha256s:
            _require_sha256(digest, "snapshot_tree_sha256")
        if (
            type(self.actor_loopback_ports) is not tuple
            or len(self.actor_loopback_ports) != 2
            or len(set(self.actor_loopback_ports)) != 2
            or any(
                type(port) is not int or not 1024 <= port <= 65535
                for port in self.actor_loopback_ports
            )
        ):
            raise ValueError("preflight must bind two distinct loopback ports")
        _require_sha256(self.pilot_task_manifest_sha256, "pilot_task_manifest_sha256")
        if type(self.smoke_fixture_sha256s) is not tuple or len(self.smoke_fixture_sha256s) != 6:
            raise ValueError("preflight must bind the six smoke fixtures")
        for digest in self.smoke_fixture_sha256s:
            _require_sha256(digest, "smoke_fixture_sha256")
        if (
            type(self.checks) is not tuple
            or not self.checks
            or any(type(check) is not ProductionPreflightCheckV1 for check in self.checks)
            or len({check.check_id for check in self.checks}) != len(self.checks)
        ):
            raise ValueError("production preflight checks are invalid")
        passed = all(check.passed for check in self.checks)
        if (
            type(cast(object, self.all_checks_passed)) is not bool
            or self.all_checks_passed is not passed
        ):
            raise ValueError("all_checks_passed differs from the check census")
        if (
            type(cast(object, self.eligible_for_post_preflight_factory)) is not bool
            or self.eligible_for_post_preflight_factory is not passed
        ):
            raise ValueError("post-preflight eligibility differs from the check census")
        if (
            type(cast(object, self.production_activation_available)) is not bool
            or self.production_activation_available is not production_activation_available_v1()
        ):
            raise ValueError("production activation declaration differs from installed code")
        for name in (
            "secret_content_reads",
            "endpoint_connections",
            "gpu_operations",
            "docker_operations",
            "model_loads",
            "backend_operations",
            "actor_actions",
            "files_written",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) != 0:
                raise ValueError("production preflight side-effect census must be zero")

    def __reduce__(self) -> tuple[object, tuple[dict[str, JsonValue]]]:
        """Rebuild the identity seal in a spawn/forkserver attempt child."""

        return (
            _restore_production_preflight_report,
            (production_preflight_report_projection(self),),
        )


def _trusted_report(value: ProductionPreflightReportV1) -> ProductionPreflightReportV1:
    if type(value) is not ProductionPreflightReportV1 or value._seal is not _REPORT_SEAL:
        raise TypeError("preflight report must be the exact module-owned type")
    return ProductionPreflightReportV1(
        schema_version=value.schema_version,
        run_id=value.run_id,
        manifest_sha256=value.manifest_sha256,
        source_commit=value.source_commit,
        checked_at_utc=value.checked_at_utc,
        authority_expires_at_utc=value.authority_expires_at_utc,
        base_preflight_sha256=value.base_preflight_sha256,
        declared_snapshot_tree_sha256s=tuple(value.declared_snapshot_tree_sha256s),
        actor_loopback_ports=tuple(value.actor_loopback_ports),
        pilot_task_manifest_sha256=value.pilot_task_manifest_sha256,
        smoke_fixture_sha256s=tuple(value.smoke_fixture_sha256s),
        checks=tuple(
            ProductionPreflightCheckV1(check.check_id, check.passed, check.verification)
            for check in value.checks
        ),
        all_checks_passed=value.all_checks_passed,
        eligible_for_post_preflight_factory=value.eligible_for_post_preflight_factory,
        production_activation_available=value.production_activation_available,
        secret_content_reads=value.secret_content_reads,
        endpoint_connections=value.endpoint_connections,
        gpu_operations=value.gpu_operations,
        docker_operations=value.docker_operations,
        model_loads=value.model_loads,
        backend_operations=value.backend_operations,
        actor_actions=value.actor_actions,
        files_written=value.files_written,
        _seal=_REPORT_SEAL,
    )


def production_preflight_report_projection(
    value: ProductionPreflightReportV1,
) -> dict[str, JsonValue]:
    trusted = _trusted_report(value)
    return {
        "actor_actions": trusted.actor_actions,
        "actor_loopback_ports": list(trusted.actor_loopback_ports),
        "all_checks_passed": trusted.all_checks_passed,
        "authority_expires_at_utc": trusted.authority_expires_at_utc,
        "backend_operations": trusted.backend_operations,
        "base_preflight_sha256": trusted.base_preflight_sha256,
        "checked_at_utc": trusted.checked_at_utc,
        "checks": [
            {
                "check_id": check.check_id,
                "passed": check.passed,
                "verification": check.verification,
            }
            for check in trusted.checks
        ],
        "declared_snapshot_tree_sha256s": list(trusted.declared_snapshot_tree_sha256s),
        "docker_operations": trusted.docker_operations,
        "eligible_for_post_preflight_factory": trusted.eligible_for_post_preflight_factory,
        "endpoint_connections": trusted.endpoint_connections,
        "files_written": trusted.files_written,
        "gpu_operations": trusted.gpu_operations,
        "manifest_sha256": trusted.manifest_sha256,
        "model_loads": trusted.model_loads,
        "pilot_task_manifest_sha256": trusted.pilot_task_manifest_sha256,
        "production_activation_available": trusted.production_activation_available,
        "run_id": trusted.run_id,
        "schema_version": trusted.schema_version,
        "secret_content_reads": trusted.secret_content_reads,
        "smoke_fixture_sha256s": list(trusted.smoke_fixture_sha256s),
        "source_commit": trusted.source_commit,
    }


def production_preflight_report_sha256(value: ProductionPreflightReportV1) -> str:
    return canonical_sha256(cast(JsonValue, production_preflight_report_projection(value)))


def _restore_production_preflight_report(
    projection: dict[str, JsonValue],
) -> ProductionPreflightReportV1:
    """Strict multiprocessing-only reconstruction of a complete report."""

    expected = {
        "actor_actions",
        "actor_loopback_ports",
        "all_checks_passed",
        "authority_expires_at_utc",
        "backend_operations",
        "base_preflight_sha256",
        "checked_at_utc",
        "checks",
        "declared_snapshot_tree_sha256s",
        "docker_operations",
        "eligible_for_post_preflight_factory",
        "endpoint_connections",
        "files_written",
        "gpu_operations",
        "manifest_sha256",
        "model_loads",
        "pilot_task_manifest_sha256",
        "production_activation_available",
        "run_id",
        "schema_version",
        "secret_content_reads",
        "smoke_fixture_sha256s",
        "source_commit",
    }
    if type(projection) is not dict or set(projection) != expected:
        raise ValueError("spawned preflight report projection differs")
    checks_value = projection["checks"]
    if type(checks_value) is not list:
        raise ValueError("spawned preflight checks differ")
    checks: list[ProductionPreflightCheckV1] = []
    for item in checks_value:
        if type(item) is not dict or set(item) != {"check_id", "passed", "verification"}:
            raise ValueError("spawned preflight check differs")
        checks.append(
            ProductionPreflightCheckV1(
                check_id=cast(str, item["check_id"]),
                passed=cast(bool, item["passed"]),
                verification=cast(str, item["verification"]),
            )
        )
    snapshot_hashes = projection["declared_snapshot_tree_sha256s"]
    ports = projection["actor_loopback_ports"]
    smoke_hashes = projection["smoke_fixture_sha256s"]
    if (
        type(snapshot_hashes) is not list
        or type(ports) is not list
        or type(smoke_hashes) is not list
    ):
        raise ValueError("spawned preflight arrays differ")
    return ProductionPreflightReportV1(
        schema_version=cast(str, projection["schema_version"]),
        run_id=cast(str, projection["run_id"]),
        manifest_sha256=cast(str, projection["manifest_sha256"]),
        source_commit=cast(str, projection["source_commit"]),
        checked_at_utc=cast(str, projection["checked_at_utc"]),
        authority_expires_at_utc=cast(str, projection["authority_expires_at_utc"]),
        base_preflight_sha256=cast(str, projection["base_preflight_sha256"]),
        declared_snapshot_tree_sha256s=tuple(cast(list[str], snapshot_hashes)),
        actor_loopback_ports=tuple(cast(list[int], ports)),
        pilot_task_manifest_sha256=cast(str, projection["pilot_task_manifest_sha256"]),
        smoke_fixture_sha256s=tuple(cast(list[str], smoke_hashes)),
        checks=tuple(checks),
        all_checks_passed=cast(bool, projection["all_checks_passed"]),
        eligible_for_post_preflight_factory=cast(
            bool, projection["eligible_for_post_preflight_factory"]
        ),
        production_activation_available=cast(bool, projection["production_activation_available"]),
        secret_content_reads=cast(int, projection["secret_content_reads"]),
        endpoint_connections=cast(int, projection["endpoint_connections"]),
        gpu_operations=cast(int, projection["gpu_operations"]),
        docker_operations=cast(int, projection["docker_operations"]),
        model_loads=cast(int, projection["model_loads"]),
        backend_operations=cast(int, projection["backend_operations"]),
        actor_actions=cast(int, projection["actor_actions"]),
        files_written=cast(int, projection["files_written"]),
        _seal=_REPORT_SEAL,
    )


@dataclass(frozen=True, slots=True)
class R24SmokeProductionPreflightReportV1:
    """Sealed, non-connecting preflight for the exact smoke-only authority."""

    schema_version: str
    execution_scope: SequenceExecutionScopeV1
    authorized_stages: tuple[RunStageV1, ...]
    run_id: str
    manifest_sha256: str
    runtime_config_sha256: str
    source_commit: str
    checked_at_utc: str
    authority_expires_at_utc: str
    declared_snapshot_tree_sha256s: tuple[str, ...]
    actor_loopback_ports: tuple[int, ...]
    smoke_fixture_sha256s: tuple[str, ...]
    checks: tuple[ProductionPreflightCheckV1, ...]
    all_checks_passed: bool
    eligible_for_post_preflight_factory: bool
    production_activation_available: bool
    secret_content_reads: int
    endpoint_connections: int
    gpu_operations: int
    docker_operations: int
    model_loads: int
    backend_operations: int
    actor_actions: int
    files_written: int
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _REPORT_SEAL:
            raise PermissionError("smoke production preflight reports are module-owned")
        if self.schema_version != R24_SMOKE_PRODUCTION_PREFLIGHT_SCHEMA_VERSION:
            raise ValueError("smoke production preflight schema differs")
        if self.execution_scope is not SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY:
            raise ValueError("smoke production preflight scope differs")
        if self.authorized_stages != (
            RunStageV1.RESOURCE_PREFLIGHT,
            RunStageV1.QWEN_LIVE_SMOKE,
            RunStageV1.MAI_LIVE_SMOKE,
        ):
            raise ValueError("smoke production preflight stage set differs")
        if type(self.run_id) is not str or _ID.fullmatch(self.run_id) is None:
            raise ValueError("smoke production preflight run ID is invalid")
        for value, name in (
            (self.manifest_sha256, "manifest_sha256"),
            (self.runtime_config_sha256, "runtime_config_sha256"),
        ):
            _require_sha256(value, name)
        if type(self.source_commit) is not str or _GIT_SHA1.fullmatch(self.source_commit) is None:
            raise ValueError("source_commit must be full lowercase SHA-1")
        _timestamp(_require_timestamp(self.checked_at_utc, "checked_at_utc"))
        _timestamp(_require_timestamp(self.authority_expires_at_utc, "authority_expires_at_utc"))
        if (
            type(self.declared_snapshot_tree_sha256s) is not tuple
            or len(self.declared_snapshot_tree_sha256s) != 2
        ):
            raise ValueError("smoke preflight must bind two snapshot tree hashes")
        for digest in self.declared_snapshot_tree_sha256s:
            _require_sha256(digest, "snapshot_tree_sha256")
        if (
            type(self.actor_loopback_ports) is not tuple
            or len(self.actor_loopback_ports) != 2
            or len(set(self.actor_loopback_ports)) != 2
            or any(
                type(port) is not int or not 1024 <= port <= 65535
                for port in self.actor_loopback_ports
            )
        ):
            raise ValueError("smoke preflight must bind two distinct loopback ports")
        if type(self.smoke_fixture_sha256s) is not tuple or len(self.smoke_fixture_sha256s) != 6:
            raise ValueError("smoke preflight must bind six fixtures")
        for digest in self.smoke_fixture_sha256s:
            _require_sha256(digest, "smoke_fixture_sha256")
        if (
            type(self.checks) is not tuple
            or not self.checks
            or any(type(check) is not ProductionPreflightCheckV1 for check in self.checks)
            or len({check.check_id for check in self.checks}) != len(self.checks)
        ):
            raise ValueError("smoke production preflight checks are invalid")
        passed = all(check.passed for check in self.checks)
        if (
            self.all_checks_passed is not passed
            or self.eligible_for_post_preflight_factory is not passed
        ):
            raise ValueError("smoke production preflight eligibility differs")
        if self.production_activation_available is not production_activation_available_v1():
            raise ValueError("production activation declaration differs")
        for name in (
            "secret_content_reads",
            "endpoint_connections",
            "gpu_operations",
            "docker_operations",
            "model_loads",
            "backend_operations",
            "actor_actions",
            "files_written",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) != 0:
                raise ValueError("smoke preflight side-effect census must be zero")

    def __reduce__(self) -> tuple[object, tuple[dict[str, JsonValue]]]:
        return (
            _restore_r24_smoke_production_preflight_report,
            (r24_smoke_production_preflight_report_projection(self),),
        )


def _trusted_smoke_report(
    value: R24SmokeProductionPreflightReportV1,
) -> R24SmokeProductionPreflightReportV1:
    if type(value) is not R24SmokeProductionPreflightReportV1 or value._seal is not _REPORT_SEAL:
        raise TypeError("smoke preflight report must be the exact module-owned type")
    return R24SmokeProductionPreflightReportV1(
        schema_version=value.schema_version,
        execution_scope=value.execution_scope,
        authorized_stages=tuple(value.authorized_stages),
        run_id=value.run_id,
        manifest_sha256=value.manifest_sha256,
        runtime_config_sha256=value.runtime_config_sha256,
        source_commit=value.source_commit,
        checked_at_utc=value.checked_at_utc,
        authority_expires_at_utc=value.authority_expires_at_utc,
        declared_snapshot_tree_sha256s=tuple(value.declared_snapshot_tree_sha256s),
        actor_loopback_ports=tuple(value.actor_loopback_ports),
        smoke_fixture_sha256s=tuple(value.smoke_fixture_sha256s),
        checks=tuple(
            ProductionPreflightCheckV1(check.check_id, check.passed, check.verification)
            for check in value.checks
        ),
        all_checks_passed=value.all_checks_passed,
        eligible_for_post_preflight_factory=value.eligible_for_post_preflight_factory,
        production_activation_available=value.production_activation_available,
        secret_content_reads=value.secret_content_reads,
        endpoint_connections=value.endpoint_connections,
        gpu_operations=value.gpu_operations,
        docker_operations=value.docker_operations,
        model_loads=value.model_loads,
        backend_operations=value.backend_operations,
        actor_actions=value.actor_actions,
        files_written=value.files_written,
        _seal=_REPORT_SEAL,
    )


def r24_smoke_production_preflight_report_projection(
    value: R24SmokeProductionPreflightReportV1,
) -> dict[str, JsonValue]:
    trusted = _trusted_smoke_report(value)
    return {
        "actor_actions": trusted.actor_actions,
        "actor_loopback_ports": list(trusted.actor_loopback_ports),
        "all_checks_passed": trusted.all_checks_passed,
        "authority_expires_at_utc": trusted.authority_expires_at_utc,
        "authorized_stages": [stage.value for stage in trusted.authorized_stages],
        "backend_operations": trusted.backend_operations,
        "checked_at_utc": trusted.checked_at_utc,
        "checks": [
            {
                "check_id": check.check_id,
                "passed": check.passed,
                "verification": check.verification,
            }
            for check in trusted.checks
        ],
        "declared_snapshot_tree_sha256s": list(trusted.declared_snapshot_tree_sha256s),
        "docker_operations": trusted.docker_operations,
        "eligible_for_post_preflight_factory": trusted.eligible_for_post_preflight_factory,
        "endpoint_connections": trusted.endpoint_connections,
        "execution_scope": trusted.execution_scope.value,
        "files_written": trusted.files_written,
        "gpu_operations": trusted.gpu_operations,
        "manifest_sha256": trusted.manifest_sha256,
        "model_loads": trusted.model_loads,
        "production_activation_available": trusted.production_activation_available,
        "run_id": trusted.run_id,
        "runtime_config_sha256": trusted.runtime_config_sha256,
        "schema_version": trusted.schema_version,
        "secret_content_reads": trusted.secret_content_reads,
        "smoke_fixture_sha256s": list(trusted.smoke_fixture_sha256s),
        "source_commit": trusted.source_commit,
    }


def r24_smoke_production_preflight_report_sha256(
    value: R24SmokeProductionPreflightReportV1,
) -> str:
    return canonical_sha256(
        cast(JsonValue, r24_smoke_production_preflight_report_projection(value))
    )


def _restore_r24_smoke_production_preflight_report(
    projection: dict[str, JsonValue],
) -> R24SmokeProductionPreflightReportV1:
    expected = {
        "actor_actions",
        "actor_loopback_ports",
        "all_checks_passed",
        "authority_expires_at_utc",
        "authorized_stages",
        "backend_operations",
        "checked_at_utc",
        "checks",
        "declared_snapshot_tree_sha256s",
        "docker_operations",
        "eligible_for_post_preflight_factory",
        "endpoint_connections",
        "execution_scope",
        "files_written",
        "gpu_operations",
        "manifest_sha256",
        "model_loads",
        "production_activation_available",
        "run_id",
        "runtime_config_sha256",
        "schema_version",
        "secret_content_reads",
        "smoke_fixture_sha256s",
        "source_commit",
    }
    if type(projection) is not dict or set(projection) != expected:
        raise ValueError("spawned smoke preflight projection differs")
    checks_value = projection.get("checks")
    stages_value = projection.get("authorized_stages")
    if type(checks_value) is not list or type(stages_value) is not list:
        raise ValueError("spawned smoke preflight collections differ")
    checks = tuple(
        ProductionPreflightCheckV1(
            check_id=cast(str, item["check_id"]),
            passed=cast(bool, item["passed"]),
            verification=cast(str, item["verification"]),
        )
        for item in checks_value
        if type(item) is dict and set(item) == {"check_id", "passed", "verification"}
    )
    if len(checks) != len(checks_value):
        raise ValueError("spawned smoke preflight checks differ")
    try:
        return R24SmokeProductionPreflightReportV1(
            schema_version=cast(str, projection["schema_version"]),
            execution_scope=SequenceExecutionScopeV1(cast(str, projection["execution_scope"])),
            authorized_stages=tuple(RunStageV1(cast(str, stage)) for stage in stages_value),
            run_id=cast(str, projection["run_id"]),
            manifest_sha256=cast(str, projection["manifest_sha256"]),
            runtime_config_sha256=cast(str, projection["runtime_config_sha256"]),
            source_commit=cast(str, projection["source_commit"]),
            checked_at_utc=cast(str, projection["checked_at_utc"]),
            authority_expires_at_utc=cast(str, projection["authority_expires_at_utc"]),
            declared_snapshot_tree_sha256s=tuple(
                cast(str, item)
                for item in cast(list[object], projection["declared_snapshot_tree_sha256s"])
            ),
            actor_loopback_ports=tuple(
                cast(int, item) for item in cast(list[object], projection["actor_loopback_ports"])
            ),
            smoke_fixture_sha256s=tuple(
                cast(str, item) for item in cast(list[object], projection["smoke_fixture_sha256s"])
            ),
            checks=checks,
            all_checks_passed=cast(bool, projection["all_checks_passed"]),
            eligible_for_post_preflight_factory=cast(
                bool, projection["eligible_for_post_preflight_factory"]
            ),
            production_activation_available=cast(
                bool, projection["production_activation_available"]
            ),
            secret_content_reads=cast(int, projection["secret_content_reads"]),
            endpoint_connections=cast(int, projection["endpoint_connections"]),
            gpu_operations=cast(int, projection["gpu_operations"]),
            docker_operations=cast(int, projection["docker_operations"]),
            model_loads=cast(int, projection["model_loads"]),
            backend_operations=cast(int, projection["backend_operations"]),
            actor_actions=cast(int, projection["actor_actions"]),
            files_written=cast(int, projection["files_written"]),
            _seal=_REPORT_SEAL,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("spawned smoke preflight projection differs") from exc


def _base_report_sha256(value: ResourcePreflightReportV1) -> str:
    return canonical_sha256(cast(JsonValue, preflight_report_projection(value)))


def _actor_loopback_ports(
    manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
) -> tuple[tuple[int, ...], tuple[ProductionPreflightCheckV1, ...]]:
    ports: list[int] = []
    checks: list[ProductionPreflightCheckV1] = []
    for resource in manifest.actor_resources:
        parsed = urlsplit(resource.actor_endpoint)
        port = parsed.port
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        passed = (
            parsed.scheme == "http"
            and loopback
            and port is not None
            and 1024 <= port <= 65535
            and parsed.path in {"", "/v1"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        if port is not None:
            ports.append(port)
        checks.append(
            ProductionPreflightCheckV1(
                f"actor_loopback_metadata:{resource.host.value}", passed, "METADATA"
            )
        )
    distinct = len(ports) == 2 and len(set(ports)) == 2
    checks.append(ProductionPreflightCheckV1("actor_loopback_ports_distinct", distinct, "METADATA"))
    # A trusted manifest already guarantees a present, bounded port for both
    # resources.  Preserve a fixed arity here even for defensive type checking.
    return tuple(ports), tuple(checks)


def _open_metadata_only_secret_binding(
    reference: SecretFileReferenceV1,
    *,
    repository_root: Path,
) -> _SecretMetadataBindingV1 | None:
    """Open and hold the secret inode without granting a readable descriptor."""

    path_only = getattr(os, "O_PATH", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if type(path_only) is not int or type(no_follow) is not int:
        return None
    descriptor = -1
    try:
        descriptor = os.open(
            reference.path,
            path_only | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        path = Path(reference.path)
        declared = path.lstat()
        resolved = path.resolve(strict=True)
        resolved_metadata = resolved.stat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != reference.required_mode
            or opened.st_uid != os.geteuid()
            or opened.st_gid != os.getegid()
            or opened.st_nlink != 1
            or not 1 <= opened.st_size <= 65_536
            or (opened.st_dev, opened.st_ino) != (declared.st_dev, declared.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (resolved_metadata.st_dev, resolved_metadata.st_ino)
            or _is_within(resolved, repository_root)
        ):
            os.close(descriptor)
            return None
        return _SecretMetadataBindingV1(
            descriptor=descriptor,
            path=path,
            resolved_path=resolved,
            device=opened.st_dev,
            inode=opened.st_ino,
        )
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        return None


def _bind_nonsecret_content_file(
    path: Path,
    *,
    secret_identity: tuple[int, int],
) -> _ContentFileBindingV1 | None:
    """Freeze regular-file metadata and reject the secret inode without reading."""

    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        resolved_metadata = resolved.stat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino)
        != (resolved_metadata.st_dev, resolved_metadata.st_ino)
        or (metadata.st_dev, metadata.st_ino) == secret_identity
    ):
        return None
    return _ContentFileBindingV1(
        path=path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        link_count=metadata.st_nlink,
        byte_count=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _opened_file_matches_binding(
    metadata: os.stat_result,
    binding: _ContentFileBindingV1,
) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == binding.device
        and metadata.st_ino == binding.inode
        and metadata.st_mode == binding.mode
        and metadata.st_uid == binding.uid
        and metadata.st_gid == binding.gid
        and metadata.st_nlink == binding.link_count
        and metadata.st_size == binding.byte_count
        and metadata.st_mtime_ns == binding.modified_ns
        and metadata.st_ctime_ns == binding.changed_ns
    )


def _read_bound_nonsecret_digest(
    binding: _ContentFileBindingV1,
    *,
    secret_identity: tuple[int, int],
) -> tuple[str, int] | None:
    """Hash only the same descriptor whose identity was checked as non-secret."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if type(no_follow) is not int:
        return None
    descriptor = -1
    try:
        descriptor = os.open(
            binding.path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not _opened_file_matches_binding(opened, binding)
            or (opened.st_dev, opened.st_ino) == secret_identity
        ):
            return None
        digest = hashlib.sha256()
        byte_count = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
        final = os.fstat(descriptor)
        if not _opened_file_matches_binding(final, binding):
            return None
        return digest.hexdigest(), byte_count
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bind_smoke_snapshot_tree(
    resource: SnapshotResourceV1,
    *,
    secret_identity: tuple[int, int],
) -> tuple[_SnapshotFileBindingV1, ...] | None:
    """Metadata-scan one snapshot without following a file or directory symlink."""

    try:
        storage_root = Path(resource.snapshot_storage_root).resolve(strict=True)
        root = Path(resource.snapshot_path).resolve(strict=True)
        if not root.is_dir() or not storage_root.is_dir() or not _is_within(root, storage_root):
            return None
        files: list[_SnapshotFileBindingV1] = []

        def _raise_walk_error(error: OSError) -> None:
            raise error

        for current, directory_names, file_names in os.walk(
            root,
            followlinks=False,
            onerror=_raise_walk_error,
        ):
            current_path = Path(current)
            for directory_name in directory_names:
                directory_metadata = (current_path / directory_name).lstat()
                if not stat.S_ISDIR(directory_metadata.st_mode):
                    return None
            for file_name in file_names:
                logical_path = current_path / file_name
                binding = _bind_nonsecret_content_file(
                    logical_path,
                    secret_identity=secret_identity,
                )
                if binding is None or not _is_within(
                    logical_path.resolve(strict=True), storage_root
                ):
                    return None
                files.append(
                    _SnapshotFileBindingV1(
                        relative_path=logical_path.relative_to(root).as_posix(),
                        content=binding,
                    )
                )
                if len(files) > 1_000_000:
                    return None
        files.sort(key=lambda item: item.relative_path.encode("utf-8"))
        return tuple(files) if files else None
    except (OSError, UnicodeError, ValueError):
        return None


def _hash_bound_smoke_snapshot_tree(
    files: tuple[_SnapshotFileBindingV1, ...],
    *,
    secret_identity: tuple[int, int],
) -> tuple[str, int, int] | None:
    aggregate = hashlib.sha256()
    aggregate.update(b"mobileworld.snapshot.logical-tree/v1\0")
    total_bytes = 0
    for item in files:
        digest = _read_bound_nonsecret_digest(
            item.content,
            secret_identity=secret_identity,
        )
        if digest is None:
            return None
        file_sha256, byte_count = digest
        relative_bytes = item.relative_path.encode("utf-8")
        aggregate.update(len(relative_bytes).to_bytes(8, "big"))
        aggregate.update(relative_bytes)
        aggregate.update(byte_count.to_bytes(16, "big"))
        aggregate.update(bytes.fromhex(file_sha256))
        total_bytes += byte_count
    return aggregate.hexdigest(), total_bytes, len(files)


def _secret_binding_is_current(binding: _SecretMetadataBindingV1) -> bool:
    try:
        opened = os.fstat(binding.descriptor)
        declared = binding.path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_IMODE(opened.st_mode) == 0o600
        and opened.st_uid == os.geteuid()
        and opened.st_gid == os.getegid()
        and opened.st_nlink == 1
        and 1 <= opened.st_size <= 65_536
        and (opened.st_dev, opened.st_ino) == (binding.device, binding.inode)
        and (declared.st_dev, declared.st_ino) == (binding.device, binding.inode)
    )


def _smoke_content_preflight_checks(
    manifest: R24SmokeRunAuthorityManifestV1,
    *,
    secret_binding: _SecretMetadataBindingV1 | None,
) -> tuple[ProductionPreflightCheckV1, ...]:
    """Admit no content read until every declared source is proven non-secret."""

    secret_identity = (
        None if secret_binding is None else (secret_binding.device, secret_binding.inode)
    )
    fixture_bindings: list[
        tuple[HostLiveSmokePlanV1, LiveSmokeCaseV1, _ContentFileBindingV1 | None]
    ] = []
    for plan in manifest.smoke_plans:
        for case in plan.cases:
            binding = (
                None
                if secret_identity is None
                else _bind_nonsecret_content_file(
                    Path(case.request_fixture_path),
                    secret_identity=secret_identity,
                )
            )
            fixture_bindings.append((plan, case, binding))

    snapshot_bindings: list[
        tuple[SnapshotResourceV1, bool, tuple[_SnapshotFileBindingV1, ...] | None]
    ] = []
    checks: list[ProductionPreflightCheckV1] = []
    for resource in manifest.actor_resources:
        metadata_ok = False
        try:
            snapshot = Path(resource.snapshot_path).resolve(strict=True)
            storage = Path(resource.snapshot_storage_root).resolve(strict=True)
            metadata_ok = snapshot.is_dir() and storage.is_dir() and _is_within(snapshot, storage)
        except OSError:
            metadata_ok = False
        files = (
            None
            if not metadata_ok or secret_identity is None
            else _bind_smoke_snapshot_tree(resource, secret_identity=secret_identity)
        )
        snapshot_bindings.append((resource, metadata_ok, files))
        checks.append(
            ProductionPreflightCheckV1(
                f"snapshot_metadata:{resource.host.value}", metadata_ok, "METADATA"
            )
        )

    fixtures_ready = all(
        binding is not None and binding.byte_count == case.request_fixture_byte_count
        for _, case, binding in fixture_bindings
    )
    snapshots_ready = all(
        metadata_ok
        and files is not None
        and len(files) == resource.snapshot_file_count
        and sum(item.content.byte_count for item in files) == resource.snapshot_total_bytes
        for resource, metadata_ok, files in snapshot_bindings
    )
    content_read_admitted = (
        secret_binding is not None
        and _secret_binding_is_current(secret_binding)
        and fixtures_ready
        and snapshots_ready
    )
    descriptor_bindings_held = content_read_admitted

    for plan, case, binding in fixture_bindings:
        content_ok = False
        if descriptor_bindings_held and binding is not None and secret_identity is not None:
            fixture_digest = _read_bound_nonsecret_digest(binding, secret_identity=secret_identity)
            if fixture_digest is None:
                descriptor_bindings_held = False
            content_ok = fixture_digest == (
                case.request_fixture_sha256,
                case.request_fixture_byte_count,
            )
        checks.append(
            ProductionPreflightCheckV1(
                f"smoke_fixture:{plan.host.value}:{case.mode.value}",
                content_ok,
                "CONTENT_SHA256",
            )
        )

    for resource, _, files in snapshot_bindings:
        content_ok = False
        if descriptor_bindings_held and files is not None and secret_identity is not None:
            snapshot_digest = _hash_bound_smoke_snapshot_tree(
                files, secret_identity=secret_identity
            )
            if snapshot_digest is None:
                descriptor_bindings_held = False
            content_ok = snapshot_digest == (
                resource.snapshot_tree_sha256,
                resource.snapshot_total_bytes,
                resource.snapshot_file_count,
            )
        checks.append(
            ProductionPreflightCheckV1(
                f"snapshot_content:{resource.host.value}", content_ok, "CONTENT_SHA256"
            )
        )
    checks.append(
        ProductionPreflightCheckV1(
            "content_read_inputs_disjoint_from_secret",
            descriptor_bindings_held,
            "METADATA",
        )
    )
    return tuple(checks)


def run_production_preflight_v1(
    manifest: R24R25RunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
    repository_root: Path,
    now: datetime | None = None,
) -> ProductionPreflightReportV1:
    """Run all CPU-verifiable production gates without connecting or reading a key."""

    if type(manifest) is not R24R25RunAuthorityManifestV1 or not isinstance(repository_root, Path):
        raise TypeError("production preflight inputs must use exact trusted types")
    trusted_manifest = parse_authority_manifest(authority_manifest_projection(manifest))
    manifest_sha256 = authority_manifest_sha256(trusted_manifest)
    _require_sha256(confirmed_manifest_sha256, "confirmed_manifest_sha256")
    if confirmed_manifest_sha256 != manifest_sha256:
        raise ValueError("confirmed owner-pinned manifest SHA-256 differs")
    current, checked_at_utc = _canonical_now(now)
    base = inspect_local_resources(
        trusted_manifest,
        repo_root=repository_root,
        deep_snapshot_hashes=True,
        now=current,
    )
    checks = [ProductionPreflightCheckV1("owner_pinned_manifest_sha256", True, "CANONICAL_SHA256")]
    checks.extend(
        ProductionPreflightCheckV1(check.check_id, check.passed, check.verification)
        for check in base.checks
    )
    ports, endpoint_checks = _actor_loopback_ports(trusted_manifest)
    checks.extend(endpoint_checks)
    checks.extend(
        (
            ProductionPreflightCheckV1(
                "owner_authority_present",
                base.owner_authority_present
                and trusted_manifest.authorization.status
                is RunAuthorizationStatusV1.OWNER_AUTHORIZED,
                "DECLARATION",
            ),
            ProductionPreflightCheckV1(
                "owner_authority_current", base.authority_current, "METADATA"
            ),
            ProductionPreflightCheckV1(
                "deep_snapshot_hashes_verified",
                base.deep_snapshot_hashes_verified,
                "CONTENT_SHA256",
            ),
            ProductionPreflightCheckV1(
                "preflight_side_effect_census_zero",
                not base.secret_content_read
                and base.network_calls == 0
                and base.gpu_operations == 0
                and base.docker_operations == 0
                and base.model_loads == 0
                and base.backend_operations == 0
                and base.actor_actions == 0
                and base.files_written == 0,
                "METADATA",
            ),
        )
    )
    all_passed = all(check.passed for check in checks)
    return ProductionPreflightReportV1(
        schema_version=PRODUCTION_PREFLIGHT_SCHEMA_VERSION,
        run_id=trusted_manifest.run_id,
        manifest_sha256=manifest_sha256,
        source_commit=trusted_manifest.source_commit,
        checked_at_utc=checked_at_utc,
        authority_expires_at_utc=trusted_manifest.authorization.expires_at_utc,
        base_preflight_sha256=_base_report_sha256(base),
        declared_snapshot_tree_sha256s=tuple(
            resource.snapshot_tree_sha256 for resource in trusted_manifest.actor_resources
        ),
        actor_loopback_ports=ports,
        pilot_task_manifest_sha256=trusted_manifest.pilot.task_manifest_sha256,
        smoke_fixture_sha256s=tuple(
            case.request_fixture_sha256
            for plan in trusted_manifest.smoke_plans
            for case in plan.cases
        ),
        checks=tuple(checks),
        all_checks_passed=all_passed,
        eligible_for_post_preflight_factory=all_passed,
        production_activation_available=production_activation_available_v1(),
        secret_content_reads=0,
        endpoint_connections=0,
        gpu_operations=0,
        docker_operations=0,
        model_loads=0,
        backend_operations=0,
        actor_actions=0,
        files_written=0,
        _seal=_REPORT_SEAL,
    )


def run_r24_smoke_production_preflight_v1(
    manifest: R24SmokeRunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
    confirmed_runtime_config_sha256: str,
    repository_root: Path,
    now: datetime | None = None,
) -> R24SmokeProductionPreflightReportV1:
    """Verify only smoke authority inputs, without resolving any R2.5 artifact."""

    if type(manifest) is not R24SmokeRunAuthorityManifestV1 or not isinstance(
        repository_root, Path
    ):
        raise TypeError("smoke production preflight inputs must use exact trusted types")
    trusted = parse_smoke_authority_manifest(smoke_authority_manifest_projection(manifest))
    manifest_sha256 = smoke_authority_manifest_sha256(trusted)
    if _require_sha256(confirmed_manifest_sha256, "confirmed_manifest_sha256") != manifest_sha256:
        raise ValueError("confirmed owner-pinned smoke manifest SHA-256 differs")
    runtime_config_sha256 = _require_sha256(
        confirmed_runtime_config_sha256, "confirmed_runtime_config_sha256"
    )
    current, checked_at_utc = _canonical_now(now)
    repository = repository_root.resolve(strict=True)
    if not repository.is_dir():
        raise ValueError("repository_root must be a directory")
    checks: list[ProductionPreflightCheckV1] = [
        ProductionPreflightCheckV1("owner_pinned_manifest_sha256", True, "CANONICAL_SHA256"),
        ProductionPreflightCheckV1(
            "owner_pinned_runtime_config_sha256",
            runtime_config_sha256 == trusted.runtime_config_sha256,
            "CANONICAL_SHA256",
        ),
        ProductionPreflightCheckV1(
            "smoke_execution_scope",
            trusted.execution_scope is SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY,
            "DECLARATION",
        ),
        ProductionPreflightCheckV1(
            "smoke_stage_set",
            trusted.safety.stages
            == (
                RunStageV1.RESOURCE_PREFLIGHT,
                RunStageV1.QWEN_LIVE_SMOKE,
                RunStageV1.MAI_LIVE_SMOKE,
            ),
            "DECLARATION",
        ),
    ]
    head, clean = _git_state(repository)
    checks.extend(
        (
            ProductionPreflightCheckV1(
                "git_source_commit", head == trusted.source_commit, "METADATA"
            ),
            ProductionPreflightCheckV1("git_worktree_clean", clean, "METADATA"),
        )
    )
    secret_binding = _open_metadata_only_secret_binding(
        trusted.secret,
        repository_root=repository,
    )
    secret_ok = secret_binding is not None
    checks.append(
        ProductionPreflightCheckV1("openai_secret_external_regular_0600", secret_ok, "METADATA")
    )
    output = Path(trusted.output_root)
    try:
        output_ok = (
            not output.exists()
            and not output.is_symlink()
            and output.parent.resolve(strict=True).is_dir()
            and not _is_within(output.resolve(strict=False), repository)
        )
    except OSError:
        output_ok = False
    checks.append(
        ProductionPreflightCheckV1("fresh_repo_external_output_root", output_ok, "METADATA")
    )
    try:
        checks.extend(
            _smoke_content_preflight_checks(
                trusted,
                secret_binding=secret_binding,
            )
        )
    finally:
        if secret_binding is not None:
            os.close(secret_binding.descriptor)
    ports, endpoint_checks = _actor_loopback_ports(trusted)
    checks.extend(endpoint_checks)
    authority_present = trusted.authorization.status is RunAuthorizationStatusV1.OWNER_AUTHORIZED
    authority_current = authority_present and _timestamp(
        trusted.authorization.issued_at_utc
    ) <= current < _timestamp(trusted.authorization.expires_at_utc)
    checks.extend(
        (
            ProductionPreflightCheckV1("owner_authority_present", authority_present, "DECLARATION"),
            ProductionPreflightCheckV1("owner_authority_current", authority_current, "METADATA"),
            ProductionPreflightCheckV1("preflight_side_effect_census_zero", True, "METADATA"),
        )
    )
    all_passed = all(check.passed for check in checks)
    return R24SmokeProductionPreflightReportV1(
        schema_version=R24_SMOKE_PRODUCTION_PREFLIGHT_SCHEMA_VERSION,
        execution_scope=trusted.execution_scope,
        authorized_stages=trusted.safety.stages,
        run_id=trusted.run_id,
        manifest_sha256=manifest_sha256,
        runtime_config_sha256=trusted.runtime_config_sha256,
        source_commit=trusted.source_commit,
        checked_at_utc=checked_at_utc,
        authority_expires_at_utc=trusted.authorization.expires_at_utc,
        declared_snapshot_tree_sha256s=tuple(
            resource.snapshot_tree_sha256 for resource in trusted.actor_resources
        ),
        actor_loopback_ports=ports,
        smoke_fixture_sha256s=tuple(
            case.request_fixture_sha256 for plan in trusted.smoke_plans for case in plan.cases
        ),
        checks=tuple(checks),
        all_checks_passed=all_passed,
        eligible_for_post_preflight_factory=all_passed,
        production_activation_available=production_activation_available_v1(),
        secret_content_reads=0,
        endpoint_connections=0,
        gpu_operations=0,
        docker_operations=0,
        model_loads=0,
        backend_operations=0,
        actor_actions=0,
        files_written=0,
        _seal=_REPORT_SEAL,
    )


@dataclass(frozen=True, slots=True)
class CaseExecutionLeaseV1:
    """Identity-sealed authority for exactly one already-assembled actor request."""

    schema_version: str
    manifest_sha256: str
    preflight_report_sha256: str
    factory_binding_sha256: str
    execution_scope: CaseExecutionScopeV1
    openai_stage_set_sha256: str
    pricing_binding_sha256: str
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
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _LEASE_SEAL:
            raise PermissionError("case execution leases are module-owned")
        if self.schema_version != CASE_EXECUTION_LEASE_SCHEMA_VERSION:
            raise ValueError("case execution lease schema differs")
        for value, name in (
            (self.manifest_sha256, "manifest_sha256"),
            (self.preflight_report_sha256, "preflight_report_sha256"),
            (self.factory_binding_sha256, "factory_binding_sha256"),
            (self.openai_stage_set_sha256, "openai_stage_set_sha256"),
            (self.pricing_binding_sha256, "pricing_binding_sha256"),
            (self.request_sha256, "request_sha256"),
        ):
            _require_sha256(value, name)
        if type(self.execution_scope) is not CaseExecutionScopeV1:
            raise ValueError("case execution lease scope is untrusted")
        if (
            self.stage
            not in {
                RunStageV1.QWEN_LIVE_SMOKE,
                RunStageV1.MAI_LIVE_SMOKE,
                RunStageV1.R25_PILOT,
            }
            or type(self.stage) is not RunStageV1
        ):
            raise ValueError("case execution lease stage is invalid")
        if type(self.host) is not PilotHostV1 or type(self.mode) is not SmokeModeV1:
            raise ValueError("case execution lease enums are untrusted")
        if type(self.case_id) is not str or _ID.fullmatch(self.case_id) is None:
            raise ValueError("case execution lease case ID is invalid")
        if type(self.task_id) is not str or _ID.fullmatch(self.task_id) is None:
            raise ValueError("case execution lease task ID is invalid")
        if self.task_parameters_sha256 is not None:
            _require_sha256(self.task_parameters_sha256, "task_parameters_sha256")
        if self.reset_seed is not None and (
            type(self.reset_seed) is not int or not 0 <= self.reset_seed <= 2_147_483_647
        ):
            raise ValueError("case execution lease reset seed is invalid")
        if (self.task_parameters_sha256 is None) != (self.reset_seed is None):
            raise ValueError("case execution lease pilot task binding is partial")
        if type(self.actor_call_index) is not int or self.actor_call_index < 1:
            raise ValueError("case execution lease actor call index is invalid")
        issued = _timestamp(_require_timestamp(self.issued_at_utc, "issued_at_utc"))
        expires = _timestamp(_require_timestamp(self.expires_at_utc, "expires_at_utc"))
        if expires <= issued:
            raise ValueError("case execution lease expiry must follow issuance")

    def __reduce__(self) -> tuple[object, tuple[dict[str, JsonValue]]]:
        """Rebuild the identity seal in a spawn/forkserver attempt child."""

        return (_restore_case_execution_lease, (case_execution_lease_projection(self),))


def _trusted_execution_lease(value: CaseExecutionLeaseV1) -> CaseExecutionLeaseV1:
    if type(value) is not CaseExecutionLeaseV1 or value._seal is not _LEASE_SEAL:
        raise TypeError("case execution lease must be the exact module-owned type")
    return CaseExecutionLeaseV1(
        schema_version=value.schema_version,
        manifest_sha256=value.manifest_sha256,
        preflight_report_sha256=value.preflight_report_sha256,
        factory_binding_sha256=value.factory_binding_sha256,
        execution_scope=value.execution_scope,
        openai_stage_set_sha256=value.openai_stage_set_sha256,
        pricing_binding_sha256=value.pricing_binding_sha256,
        stage=value.stage,
        host=value.host,
        mode=value.mode,
        case_id=value.case_id,
        task_id=value.task_id,
        task_parameters_sha256=value.task_parameters_sha256,
        reset_seed=value.reset_seed,
        actor_call_index=value.actor_call_index,
        request_sha256=value.request_sha256,
        issued_at_utc=value.issued_at_utc,
        expires_at_utc=value.expires_at_utc,
        _seal=_LEASE_SEAL,
    )


def case_execution_lease_projection(value: CaseExecutionLeaseV1) -> dict[str, JsonValue]:
    trusted = _trusted_execution_lease(value)
    return {
        "case_id": trusted.case_id,
        "actor_call_index": trusted.actor_call_index,
        "expires_at_utc": trusted.expires_at_utc,
        "execution_scope": trusted.execution_scope.value,
        "factory_binding_sha256": trusted.factory_binding_sha256,
        "openai_stage_set_sha256": trusted.openai_stage_set_sha256,
        "pricing_binding_sha256": trusted.pricing_binding_sha256,
        "host": trusted.host.value,
        "issued_at_utc": trusted.issued_at_utc,
        "manifest_sha256": trusted.manifest_sha256,
        "mode": trusted.mode.value,
        "preflight_report_sha256": trusted.preflight_report_sha256,
        "request_sha256": trusted.request_sha256,
        "reset_seed": trusted.reset_seed,
        "schema_version": trusted.schema_version,
        "stage": trusted.stage.value,
        "task_id": trusted.task_id,
        "task_parameters_sha256": trusted.task_parameters_sha256,
    }


def _restore_case_execution_lease(
    projection: dict[str, JsonValue],
) -> CaseExecutionLeaseV1:
    expected = {
        "actor_call_index",
        "case_id",
        "execution_scope",
        "expires_at_utc",
        "factory_binding_sha256",
        "host",
        "issued_at_utc",
        "manifest_sha256",
        "mode",
        "openai_stage_set_sha256",
        "preflight_report_sha256",
        "pricing_binding_sha256",
        "request_sha256",
        "reset_seed",
        "schema_version",
        "stage",
        "task_id",
        "task_parameters_sha256",
    }
    if type(projection) is not dict or set(projection) != expected:
        raise ValueError("spawned case execution lease projection differs")
    return CaseExecutionLeaseV1(
        schema_version=cast(str, projection["schema_version"]),
        manifest_sha256=cast(str, projection["manifest_sha256"]),
        preflight_report_sha256=cast(str, projection["preflight_report_sha256"]),
        factory_binding_sha256=cast(str, projection["factory_binding_sha256"]),
        execution_scope=CaseExecutionScopeV1(cast(str, projection["execution_scope"])),
        openai_stage_set_sha256=cast(str, projection["openai_stage_set_sha256"]),
        pricing_binding_sha256=cast(str, projection["pricing_binding_sha256"]),
        stage=RunStageV1(cast(str, projection["stage"])),
        host=PilotHostV1(cast(str, projection["host"])),
        mode=SmokeModeV1(cast(str, projection["mode"])),
        case_id=cast(str, projection["case_id"]),
        task_id=cast(str, projection["task_id"]),
        task_parameters_sha256=cast(str | None, projection["task_parameters_sha256"]),
        reset_seed=cast(int | None, projection["reset_seed"]),
        actor_call_index=cast(int, projection["actor_call_index"]),
        request_sha256=cast(str, projection["request_sha256"]),
        issued_at_utc=cast(str, projection["issued_at_utc"]),
        expires_at_utc=cast(str, projection["expires_at_utc"]),
        _seal=_LEASE_SEAL,
    )


def case_execution_lease_sha256(value: CaseExecutionLeaseV1) -> str:
    return canonical_sha256(cast(JsonValue, case_execution_lease_projection(value)))


def _detach_openai_stage(value: OpenAIResponsesStageV1) -> OpenAIResponsesStageV1:
    if type(value) is not OpenAIResponsesStageV1:
        raise TypeError("OpenAI stage must use the exact trusted type")
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


def openai_stage_projection(value: OpenAIResponsesStageV1) -> dict[str, JsonValue]:
    trusted = _detach_openai_stage(value)
    return {
        "endpoint": trusted.endpoint,
        "external_network_on_call": trusted.external_network_on_call,
        "max_attempts": trusted.max_attempts,
        "max_output_tokens": trusted.max_output_tokens,
        "model": trusted.model,
        "model_on_call": trusted.model_on_call,
        "openai_sdk_version": trusted.openai_sdk_version,
        "role": trusted.role.value,
        "sdk_max_retries": trusted.sdk_max_retries,
        "store": trusted.store,
        "timeout_ms": trusted.timeout_ms,
        "transport_authority": trusted.transport_authority,
        "transport_kind": trusted.transport_kind,
    }


def openai_stage_sha256(value: OpenAIResponsesStageV1) -> str:
    return canonical_sha256(cast(JsonValue, openai_stage_projection(value)))


def _trusted_openai_stages(
    manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
) -> tuple[OpenAIResponsesStageV1, ...]:
    if type(manifest.openai_stages) is not tuple or not manifest.openai_stages:
        raise ValueError("manifest must declare OpenAI stages")
    roles = tuple(stage.role for stage in manifest.openai_stages)
    if (
        len(set(roles)) != len(roles)
        or OpenAIRoleV1.HISTORY_POLICY not in roles
        or any(role not in {OpenAIRoleV1.RUBRIC, OpenAIRoleV1.HISTORY_POLICY} for role in roles)
    ):
        raise ValueError("manifest OpenAI stage roles differ")
    return tuple(_detach_openai_stage(stage) for stage in manifest.openai_stages)


def openai_stage_set_sha256(values: tuple[OpenAIResponsesStageV1, ...]) -> str:
    if type(values) is not tuple or not values:
        raise TypeError("OpenAI stage set must be a nonempty exact tuple")
    trusted = tuple(_detach_openai_stage(value) for value in values)
    if len({value.role for value in trusted}) != len(trusted):
        raise ValueError("OpenAI stage set repeats a role")
    return canonical_sha256(
        cast(JsonValue, {"openai_stages": [openai_stage_projection(value) for value in trusted]})
    )


class _ProductionActivationCapabilityV1:
    """Unforgeable-by-contract identity capability owned by this module."""

    __slots__ = ()

    def __init__(self, *, seal: object) -> None:
        if seal is not _CAPABILITY_CONSTRUCTION_SEAL:
            raise PermissionError("production capability construction is module-owned")


# The implementation capability is module-owned.  It grants no run authority:
# the owner manifest, sealed preflight, exact case lease, and pricing pin remain
# mandatory for every child attempt.
_INSTALLED_PRODUCTION_CAPABILITY: Final[_ProductionActivationCapabilityV1] = (
    _ProductionActivationCapabilityV1(seal=_CAPABILITY_CONSTRUCTION_SEAL)
)


def production_activation_available_v1() -> bool:
    return True


class SecureOpenAISecretLeaseV1:
    """Opaque, zero-on-close secret bytes bound to one case execution lease.

    There is intentionally no public value accessor.  The reviewed child-only
    OpenAI attempt worker may borrow the value through the same private identity
    capability that created this lease.
    """

    __slots__ = (
        "_buffer",
        "_capability",
        "_case_execution_lease_sha256",
        "_closed",
        "_environment_key",
        "_lock",
        "_manifest_sha256",
        "_preflight_report_sha256",
    )

    def __init__(
        self,
        reference: SecretFileReferenceV1,
        *,
        case_lease: CaseExecutionLeaseV1,
        capability: _ProductionActivationCapabilityV1,
    ) -> None:
        installed = _INSTALLED_PRODUCTION_CAPABILITY
        if capability is not installed:
            raise PermissionError("production secret activation is unavailable")
        if type(reference) is not SecretFileReferenceV1:
            raise TypeError("secret reference must use the exact trusted type")
        trusted_lease = _trusted_execution_lease(case_lease)
        payload = self._read_exact_secret(reference)
        self._buffer = payload
        self._capability = capability
        self._case_execution_lease_sha256 = case_execution_lease_sha256(trusted_lease)
        self._closed = False
        self._environment_key = reference.environment_key
        self._lock = threading.Lock()
        self._manifest_sha256 = trusted_lease.manifest_sha256
        self._preflight_report_sha256 = trusted_lease.preflight_report_sha256

    @staticmethod
    def _read_exact_secret(reference: SecretFileReferenceV1) -> bytearray:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if type(no_follow) is not int:
            raise PermissionError("platform lacks no-follow secret opening")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow
        descriptor = -1
        buffer = bytearray()
        try:
            descriptor = os.open(reference.path, flags)
            opened = os.fstat(descriptor)
            declared = os.lstat(reference.path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != reference.required_mode
                or opened.st_uid != os.geteuid()
                or opened.st_gid != os.getegid()
                or opened.st_nlink != 1
                or opened.st_dev != declared.st_dev
                or opened.st_ino != declared.st_ino
                or declared.st_nlink != 1
                or not 1 <= opened.st_size <= 65_536
            ):
                raise PermissionError("secret metadata changed after preflight")
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 4096))
                if not chunk:
                    raise PermissionError("secret file changed while leased")
                buffer.extend(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise PermissionError("secret file grew while leased")
            final = os.fstat(descriptor)
            if (
                final.st_dev != opened.st_dev
                or final.st_ino != opened.st_ino
                or final.st_size != opened.st_size
                or final.st_mtime_ns != opened.st_mtime_ns
                or final.st_nlink != 1
            ):
                raise PermissionError("secret metadata changed while leased")
            while buffer and buffer[-1] in {10, 13}:
                buffer.pop()
            if not buffer or 0 in buffer or 10 in buffer or 13 in buffer:
                raise PermissionError("secret must be one nonempty raw line")
            try:
                buffer.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise PermissionError("secret must be UTF-8 text") from exc
            return buffer
        except Exception:
            for index in range(len(buffer)):
                buffer[index] = 0
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    @property
    def preflight_report_sha256(self) -> str:
        return self._preflight_report_sha256

    @property
    def case_execution_lease_sha256(self) -> str:
        return self._case_execution_lease_sha256

    @property
    def environment_key(self) -> str:
        return self._environment_key

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _borrow_for_openai_client(self, capability: _ProductionActivationCapabilityV1) -> str:
        with self._lock:
            if capability is not self._capability or self._closed:
                raise PermissionError("secret lease is unavailable")
            return self._buffer.decode("utf-8", errors="strict")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for index in range(len(self._buffer)):
                self._buffer[index] = 0
            self._closed = True

    def __repr__(self) -> str:
        return (
            "SecureOpenAISecretLeaseV1("
            f"manifest_sha256={self._manifest_sha256!r}, "
            f"preflight_report_sha256={self._preflight_report_sha256!r}, "
            f"case_execution_lease_sha256={self._case_execution_lease_sha256!r}, "
            f"environment_key={self._environment_key!r}, closed={self.closed!r})"
        )


class ProductionPostPreflightFactoryV1:
    """Module-owned issuer rooted in one confirmed manifest and sealed preflight."""

    __slots__ = (
        "_capability",
        "_creator_pid",
        "_factory_binding_sha256",
        "_manifest",
        "_manifest_sha256",
        "_openai_stage_set_sha256",
        "_openai_stages",
        "_preflight_report_sha256",
        "_pricing_binding_sha256",
        "_report",
        "_runtime_config_sha256",
        "_sequence_execution_scope",
    )

    def __init__(
        self,
        manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
        report: ProductionPreflightReportV1 | R24SmokeProductionPreflightReportV1,
        *,
        confirmed_manifest_sha256: str,
        confirmed_preflight_report_sha256: str,
        confirmed_pricing_sha256: str,
        capability: _ProductionActivationCapabilityV1,
        seal: object,
    ) -> None:
        installed = _INSTALLED_PRODUCTION_CAPABILITY
        if seal is not _FACTORY_SEAL or capability is not installed:
            raise PermissionError("production post-preflight factory is unavailable")
        if (
            type(manifest) is R24R25RunAuthorityManifestV1
            and type(report) is ProductionPreflightReportV1
        ):
            legacy_manifest = manifest
            legacy_report = report
            trusted_manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1 = (
                parse_authority_manifest(authority_manifest_projection(legacy_manifest))
            )
            trusted_report: ProductionPreflightReportV1 | R24SmokeProductionPreflightReportV1 = (
                _trusted_report(legacy_report)
            )
            manifest_sha256 = authority_manifest_sha256(
                cast(R24R25RunAuthorityManifestV1, trusted_manifest)
            )
            report_sha256 = production_preflight_report_sha256(
                cast(ProductionPreflightReportV1, trusted_report)
            )
            sequence_scope = SequenceExecutionScopeV1.R24_R25_FULL
            runtime_config_sha256: str | None = None
        elif (
            type(manifest) is R24SmokeRunAuthorityManifestV1
            and type(report) is R24SmokeProductionPreflightReportV1
        ):
            smoke_manifest = manifest
            smoke_report = report
            trusted_manifest = parse_smoke_authority_manifest(
                smoke_authority_manifest_projection(smoke_manifest)
            )
            trusted_report = _trusted_smoke_report(smoke_report)
            manifest_sha256 = smoke_authority_manifest_sha256(trusted_manifest)
            report_sha256 = r24_smoke_production_preflight_report_sha256(trusted_report)
            sequence_scope = SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY
            runtime_config_sha256 = trusted_manifest.runtime_config_sha256
            if (
                trusted_report.execution_scope is not sequence_scope
                or trusted_report.authorized_stages != trusted_manifest.safety.stages
                or trusted_report.runtime_config_sha256 != runtime_config_sha256
            ):
                raise ValueError("smoke post-preflight scope bindings differ")
        else:
            raise TypeError("post-preflight manifest/report schemas do not match")
        if (
            confirmed_manifest_sha256 != manifest_sha256
            or confirmed_preflight_report_sha256 != report_sha256
            or trusted_report.manifest_sha256 != manifest_sha256
            or not trusted_report.eligible_for_post_preflight_factory
        ):
            raise ValueError("post-preflight factory bindings differ")
        self._capability = capability
        self._creator_pid = os.getpid()
        self._manifest = trusted_manifest
        self._report = trusted_report
        self._manifest_sha256 = manifest_sha256
        self._preflight_report_sha256 = report_sha256
        self._openai_stages = _trusted_openai_stages(trusted_manifest)
        self._openai_stage_set_sha256 = openai_stage_set_sha256(self._openai_stages)
        self._pricing_binding_sha256 = _require_sha256(
            confirmed_pricing_sha256, "confirmed_pricing_sha256"
        )
        self._sequence_execution_scope = sequence_scope
        self._runtime_config_sha256 = runtime_config_sha256
        binding: dict[str, JsonValue] = {
            "authorization_id": trusted_manifest.authorization.authorization_id,
            "execution_scope": CaseExecutionScopeV1.OWNER_AUTHORIZED_LIVE.value,
            "openai_stage_set_sha256": self._openai_stage_set_sha256,
            "pricing_binding_sha256": self._pricing_binding_sha256,
            "manifest_sha256": manifest_sha256,
            "preflight_report_sha256": report_sha256,
            "run_id": trusted_manifest.run_id,
            "source_commit": trusted_manifest.source_commit,
        }
        if sequence_scope is SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY:
            assert runtime_config_sha256 is not None
            binding.update(
                {
                    "authorized_stages": [stage.value for stage in trusted_manifest.safety.stages],
                    "runtime_config_sha256": runtime_config_sha256,
                    "sequence_execution_scope": sequence_scope.value,
                }
            )
        self._factory_binding_sha256 = canonical_sha256(binding)

    def __reduce__(
        self,
    ) -> tuple[
        object,
        tuple[
            R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
            ProductionPreflightReportV1 | R24SmokeProductionPreflightReportV1,
            str,
            str,
            str,
            int,
        ],
    ]:
        """Reconstitute module seals in a clean multiprocessing child."""

        return (
            _restore_post_preflight_factory_for_child,
            (
                self.manifest_snapshot(),
                (
                    _trusted_report(self._report)
                    if type(self._report) is ProductionPreflightReportV1
                    else _trusted_smoke_report(
                        cast(R24SmokeProductionPreflightReportV1, self._report)
                    )
                ),
                self._manifest_sha256,
                self._preflight_report_sha256,
                self._pricing_binding_sha256,
                self._creator_pid,
            ),
        )

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    def manifest_snapshot(
        self,
    ) -> R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1:
        """Return detached authority metadata; referenced secret bytes are never read."""

        if type(self._manifest) is R24R25RunAuthorityManifestV1:
            return parse_authority_manifest(authority_manifest_projection(self._manifest))
        if type(self._manifest) is R24SmokeRunAuthorityManifestV1:
            return parse_smoke_authority_manifest(
                smoke_authority_manifest_projection(self._manifest)
            )
        raise TypeError("post-preflight manifest type differs")

    @property
    def sequence_execution_scope(self) -> SequenceExecutionScopeV1:
        return self._sequence_execution_scope

    @property
    def runtime_config_sha256(self) -> str | None:
        return self._runtime_config_sha256

    @property
    def preflight_report_sha256(self) -> str:
        return self._preflight_report_sha256

    @property
    def factory_binding_sha256(self) -> str:
        return self._factory_binding_sha256

    def openai_stage(self, role: OpenAIRoleV1) -> OpenAIResponsesStageV1:
        if type(role) is not OpenAIRoleV1:
            raise TypeError("OpenAI stage role is untrusted")
        stage = next((value for value in self._openai_stages if value.role is role), None)
        if stage is None:
            raise ValueError("OpenAI stage role is absent from the owner manifest")
        return _detach_openai_stage(stage)

    @property
    def openai_stage_set_sha256(self) -> str:
        return self._openai_stage_set_sha256

    @property
    def pricing_binding_sha256(self) -> str:
        return self._pricing_binding_sha256

    def openai_stage_sha256(self, role: OpenAIRoleV1) -> str:
        return openai_stage_sha256(self.openai_stage(role))

    def _case_bound(
        self,
        stage: RunStageV1,
        host: PilotHostV1,
        mode: SmokeModeV1,
        case_id: str,
    ) -> tuple[bool, int, str | None, str | None, int | None, int]:
        if stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}:
            expected_stage = (
                RunStageV1.QWEN_LIVE_SMOKE
                if host is PilotHostV1.QWEN3_VL
                else RunStageV1.MAI_LIVE_SMOKE
            )
            plan = next((plan for plan in self._manifest.smoke_plans if plan.host is host), None)
            case = next(
                (
                    item
                    for item in (() if plan is None else plan.cases)
                    if item.case_id == case_id and item.mode is mode
                ),
                None,
            )
            return (
                stage is expected_stage and case is not None and mode is not SmokeModeV1.OFF,
                0 if case is None else case.max_wall_time_seconds,
                None if case is None else case.task_id,
                None,
                None,
                0 if case is None else case.max_actor_calls,
            )
        if stage is not RunStageV1.R25_PILOT or mode is not SmokeModeV1.ACTIVE:
            return False, 0, None, None, None, 0
        if type(self._manifest) is R24SmokeRunAuthorityManifestV1:
            return False, 0, None, None, None, 0
        joint_manifest = cast(R24R25RunAuthorityManifestV1, self._manifest)
        if not case_id.startswith("pilot-cell-"):
            return False, 0, None, None, None, 0
        index_text = case_id.removeprefix("pilot-cell-")
        if len(index_text) != 3 or not index_text.isascii() or not index_text.isdigit():
            return False, 0, None, None, None, 0
        try:
            index = int(index_text)
            cell = joint_manifest.pilot.cells[index]
        except (ValueError, IndexError):
            return False, 0, None, None, None, 0
        expected_mode = SmokeModeV1.OFF if cell.sentinel_mode == "OFF" else SmokeModeV1.ACTIVE
        return (
            cell.host is host and expected_mode is mode,
            joint_manifest.pilot.per_cell_timeout_seconds,
            cell.task_id,
            cell.task_parameters_sha256,
            cell.reset_seed,
            joint_manifest.pilot.max_steps_per_cell,
        )

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
        now: datetime | None = None,
    ) -> CaseExecutionLeaseV1:
        current, issued_at_utc = _canonical_now(now)
        (
            valid_case,
            max_seconds,
            expected_task_id,
            expected_task_parameters_sha256,
            expected_reset_seed,
            max_actor_calls,
        ) = self._case_bound(stage, host, mode, case_id)
        if (
            not valid_case
            or task_id != expected_task_id
            or task_parameters_sha256 != expected_task_parameters_sha256
            or reset_seed != expected_reset_seed
            or type(actor_call_index) is not int
            or not 1 <= actor_call_index <= max_actor_calls
        ):
            raise ValueError("requested case is outside the owner-pinned manifest")
        _require_sha256(request_sha256, "request_sha256")
        authority_expiry = _timestamp(self._manifest.authorization.expires_at_utc)
        expiry = min(authority_expiry, current + timedelta(seconds=max_seconds))
        if current >= expiry:
            raise ValueError("owner authority has expired")
        return CaseExecutionLeaseV1(
            schema_version=CASE_EXECUTION_LEASE_SCHEMA_VERSION,
            manifest_sha256=self._manifest_sha256,
            preflight_report_sha256=self._preflight_report_sha256,
            factory_binding_sha256=self._factory_binding_sha256,
            execution_scope=CaseExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
            openai_stage_set_sha256=self._openai_stage_set_sha256,
            pricing_binding_sha256=self._pricing_binding_sha256,
            stage=stage,
            host=host,
            mode=mode,
            case_id=case_id,
            task_id=task_id,
            task_parameters_sha256=task_parameters_sha256,
            reset_seed=reset_seed,
            actor_call_index=actor_call_index,
            request_sha256=request_sha256,
            issued_at_utc=issued_at_utc,
            expires_at_utc=expiry.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            _seal=_LEASE_SEAL,
        )

    def validate_case_execution_lease(
        self,
        case_lease: CaseExecutionLeaseV1,
        *,
        now: datetime | None = None,
    ) -> CaseExecutionLeaseV1:
        """Return a detached, current lease belonging exactly to this factory."""

        trusted = _trusted_execution_lease(case_lease)
        current, _ = _canonical_now(now)
        if (
            trusted.manifest_sha256 != self._manifest_sha256
            or trusted.preflight_report_sha256 != self._preflight_report_sha256
            or trusted.factory_binding_sha256 != self._factory_binding_sha256
            or trusted.openai_stage_set_sha256 != self._openai_stage_set_sha256
            or trusted.pricing_binding_sha256 != self._pricing_binding_sha256
        ):
            raise ValueError("case execution lease belongs to another authority")
        (
            valid_case,
            _,
            expected_task_id,
            expected_task_parameters_sha256,
            expected_reset_seed,
            max_actor_calls,
        ) = self._case_bound(
            trusted.stage,
            trusted.host,
            trusted.mode,
            trusted.case_id,
        )
        if (
            not valid_case
            or trusted.task_id != expected_task_id
            or trusted.task_parameters_sha256 != expected_task_parameters_sha256
            or trusted.reset_seed != expected_reset_seed
            or not 1 <= trusted.actor_call_index <= max_actor_calls
        ):
            raise ValueError("case execution lease is outside the owner manifest")
        if current < _timestamp(trusted.issued_at_utc) or current >= _timestamp(
            trusted.expires_at_utc
        ):
            raise ValueError("case execution lease is not currently valid")
        return _trusted_execution_lease(trusted)

    def acquire_secret_lease(
        self,
        case_lease: CaseExecutionLeaseV1,
    ) -> SecureOpenAISecretLeaseV1:
        if os.getpid() == self._creator_pid:
            raise PermissionError("OpenAI secret lease is child-process-only")
        trusted = self.validate_case_execution_lease(case_lease)
        return SecureOpenAISecretLeaseV1(
            self._manifest.secret,
            case_lease=trusted,
            capability=self._capability,
        )

    def _acquire_openai_secret_for_child_process(
        self,
        case_lease: CaseExecutionLeaseV1,
    ) -> tuple[SecureOpenAISecretLeaseV1, str]:
        """Borrow the secret only in a spawned, module-owned attempt worker."""

        lease = self.acquire_secret_lease(case_lease)
        try:
            value = lease._borrow_for_openai_client(self._capability)
        except Exception:
            lease.close()
            raise
        return lease, value


def _restore_post_preflight_factory_for_child(
    manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
    report: ProductionPreflightReportV1 | R24SmokeProductionPreflightReportV1,
    manifest_sha256: str,
    preflight_report_sha256: str,
    pricing_binding_sha256: str,
    creator_pid: int,
) -> ProductionPostPreflightFactoryV1:
    """Spawn/forkserver reconstruction retaining the original parent PID gate."""

    if type(creator_pid) is not int or creator_pid <= 0 or creator_pid == os.getpid():
        raise PermissionError("production child factory parent identity differs")
    restored = ProductionPostPreflightFactoryV1(
        manifest,
        report,
        confirmed_manifest_sha256=manifest_sha256,
        confirmed_preflight_report_sha256=preflight_report_sha256,
        confirmed_pricing_sha256=pricing_binding_sha256,
        capability=_INSTALLED_PRODUCTION_CAPABILITY,
        seal=_FACTORY_SEAL,
    )
    restored._creator_pid = creator_pid
    return restored


def require_production_post_preflight_factory_v1(
    manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
    report: ProductionPreflightReportV1 | R24SmokeProductionPreflightReportV1,
    *,
    confirmed_manifest_sha256: str,
    confirmed_preflight_report_sha256: str,
    confirmed_pricing_sha256: str,
) -> ProductionPostPreflightFactoryV1:
    """Create the sole production issuer after the exact preflight chain passes."""

    return ProductionPostPreflightFactoryV1(
        manifest,
        report,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
        confirmed_preflight_report_sha256=confirmed_preflight_report_sha256,
        confirmed_pricing_sha256=confirmed_pricing_sha256,
        capability=_INSTALLED_PRODUCTION_CAPABILITY,
        seal=_FACTORY_SEAL,
    )


__all__ = [
    "CASE_EXECUTION_LEASE_SCHEMA_VERSION",
    "PRODUCTION_PREFLIGHT_SCHEMA_VERSION",
    "CaseExecutionLeaseV1",
    "CaseExecutionScopeV1",
    "ProductionPostPreflightFactoryV1",
    "ProductionPreflightCheckV1",
    "ProductionPreflightReportV1",
    "R24_SMOKE_PRODUCTION_PREFLIGHT_SCHEMA_VERSION",
    "R24SmokeProductionPreflightReportV1",
    "SecureOpenAISecretLeaseV1",
    "case_execution_lease_projection",
    "case_execution_lease_sha256",
    "openai_stage_projection",
    "openai_stage_set_sha256",
    "openai_stage_sha256",
    "production_activation_available_v1",
    "production_preflight_report_projection",
    "production_preflight_report_sha256",
    "r24_smoke_production_preflight_report_projection",
    "r24_smoke_production_preflight_report_sha256",
    "require_production_post_preflight_factory_v1",
    "run_production_preflight_v1",
    "run_r24_smoke_production_preflight_v1",
]
