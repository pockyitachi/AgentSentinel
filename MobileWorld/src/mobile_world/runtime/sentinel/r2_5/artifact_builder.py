"""Deterministic, CPU-only R2.4/R2.5 authority artifact construction.

The builder reads a declared GUI-only task source, current MobileWorld task
metadata, and non-secret fixture bytes.  It never reads a credential, probes a
GPU, uses the network, starts Docker, or executes MobileWorld.  Its authority
manifest is deliberately emitted as ``DRAFT_NOT_AUTHORIZED``; a later owner
authorization must be explicit and hash-bound.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes, canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
    SNAPSHOT_TREE_ALGORITHM_V1,
    HostLiveSmokePlanV1,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    OwnerAuthorizationV1,
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SequenceSafetyV1,
    SmokeModeV1,
    SnapshotResourceV1,
    authority_manifest_projection,
    authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_4.topology_artifact import (
    R24CpuTopologyArtifactV1,
    parse_r24_cpu_topology_artifact,
    r24_cpu_topology_artifact_projection,
    r24_cpu_topology_artifact_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION,
    FROZEN_PILOT_SCHEMA_VERSION,
    FrozenPilotManifestV1,
    InlinePilotTaskParametersV1,
    MobileWorldTaskParametersV1,
    PilotArmV1,
    PilotHostV1,
    PilotSeedPolicyV1,
    PilotTaskTimeAuthorityV1,
    PilotTaskV1,
    PilotTopologyV1,
    executable_pilot_task_source_projection,
    frozen_pilot_manifest_projection,
    frozen_pilot_manifest_sha256,
)

ARTIFACT_BUNDLE_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-r2.5-artifacts/v1"
COHORT_SELECTION_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.5-cohort-selection/v1"
COHORT_SELECTION_ALGORITHM = "SHA256_R25_PILOT_V1"
TASK_TIME_DEPENDENCY_AUDIT_ALGORITHM = "PYTHON_SOURCE_WALL_CLOCK_SCAN_V1"
GUI_ONLY_TASK_SOURCE_FILENAME = "gui-only-task-source.jsonl"
COHORT_SELECTION_FILENAME = "cohort-selection.v1.json"
PILOT_TASK_SOURCE_FILENAME = "pilot-task-source.json"
FROZEN_PILOT_MANIFEST_FILENAME = "frozen-pilot-manifest.json"
RUN_AUTHORITY_MANIFEST_FILENAME = "run-authority-manifest.draft.json"
ARTIFACT_BUNDLE_FILENAME = "artifact-bundle.json"
TOPOLOGY_COMPARISON_FILENAME = "cpu-topology-comparison.v1.json"

_SELECTION_DOMAIN = b"r25-pilot-v1\0"
_RESET_SEED_DOMAIN = b"r25-reset-seed-v1\0"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}")
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_ROWS = 10_000
_MAX_FIXTURE_BYTES = 100_000_000
_MAX_TOPOLOGY_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_TASK_DEFINITION_BYTES = 4 * 1024 * 1024
_DYNAMIC_TIME_APPS = frozenset({"Chrome", "Maps", "MCP-arXiv"})
_DYNAMIC_TIME_SOURCE_MARKERS = (
    b"datetime.now",
    b"datetime.datetime.now",
    b".today(",
    b".utcnow(",
    b"date.today",
    b"time.time(",
    b"time_sync_to_now",
    b"enable_auto_time_sync",
    b"get_device_datetime",
    b"get_device_date",
)


class R25ArtifactBuildError(ValueError):
    """Closed, value-free failure raised by the offline artifact builder."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class RegistryTaskTimeDependencyV1(StrEnum):
    STATIC_WALL_CLOCK_INDEPENDENT = "STATIC_WALL_CLOCK_INDEPENDENT"
    DYNAMIC_OR_UNKNOWN_WALL_CLOCK = "DYNAMIC_OR_UNKNOWN_WALL_CLOCK"


class CohortTaskAuditDispositionV1(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    EXCLUDED_MISSING_REGISTRY = "EXCLUDED_MISSING_REGISTRY"
    EXCLUDED_USER_INTERACTION = "EXCLUDED_USER_INTERACTION"
    EXCLUDED_MCP = "EXCLUDED_MCP"
    EXCLUDED_DYNAMIC_OR_UNKNOWN_WALL_CLOCK = "EXCLUDED_DYNAMIC_OR_UNKNOWN_WALL_CLOCK"


@dataclass(frozen=True, slots=True)
class RegistryTaskMetadataV1:
    task_id: str
    task_tags: tuple[str, ...]
    app_names: tuple[str, ...]
    task_time_dependency: RegistryTaskTimeDependencyV1
    definition_source_sha256: str

    def __post_init__(self) -> None:
        MobileWorldTaskParametersV1(task_name=self.task_id, trial=1)
        for value, name in ((self.task_tags, "task_tags"), (self.app_names, "app_names")):
            if type(value) is not tuple or any(type(item) is not str for item in value):
                raise R25ArtifactBuildError("INVALID_REGISTRY_METADATA", f"{name} is invalid")
            if tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))) != value:
                raise R25ArtifactBuildError(
                    "NONCANONICAL_REGISTRY_METADATA", f"{name} is not sorted and unique"
                )
        if type(self.task_time_dependency) is not RegistryTaskTimeDependencyV1:
            raise R25ArtifactBuildError(
                "INVALID_REGISTRY_METADATA", "task_time_dependency is untrusted"
            )
        if (
            type(self.definition_source_sha256) is not str
            or _SHA256.fullmatch(self.definition_source_sha256) is None
        ):
            raise R25ArtifactBuildError(
                "INVALID_REGISTRY_METADATA", "task definition source digest is invalid"
            )


@dataclass(frozen=True, slots=True)
class CohortMemberV1:
    task_id: str
    trial: int
    selection_sha256: str
    reset_seed: int
    task_parameters_sha256: str

    def __post_init__(self) -> None:
        MobileWorldTaskParametersV1(task_name=self.task_id, trial=self.trial)
        for value in (self.selection_sha256, self.task_parameters_sha256):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise R25ArtifactBuildError("INVALID_DIGEST", "cohort digest is invalid")
        PilotTaskV1(
            task_id=self.task_id,
            task_parameters_sha256=self.task_parameters_sha256,
            reset_seed=self.reset_seed,
        )
        if self.reset_seed < 1:
            raise R25ArtifactBuildError("INVALID_SELECTION", "selected reset seed must be positive")


@dataclass(frozen=True, slots=True)
class CohortTaskAuditRecordV1:
    source_row_index: int
    task_id: str
    trial: int
    disposition: CohortTaskAuditDispositionV1
    definition_source_sha256: str | None
    selection_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.source_row_index) is not int or not 1 <= self.source_row_index <= 10_000:
            raise R25ArtifactBuildError(
                "INVALID_SOURCE_ROW_INDEX", "source row index is outside bounds"
            )
        MobileWorldTaskParametersV1(task_name=self.task_id, trial=self.trial)
        if type(self.disposition) is not CohortTaskAuditDispositionV1:
            raise R25ArtifactBuildError(
                "INVALID_AUDIT_DISPOSITION", "source task disposition is untrusted"
            )
        if self.disposition is CohortTaskAuditDispositionV1.EXCLUDED_MISSING_REGISTRY:
            if self.definition_source_sha256 is not None:
                raise R25ArtifactBuildError(
                    "INVALID_AUDIT_BINDING",
                    "missing-registry rows cannot bind a definition source",
                )
        elif (
            type(self.definition_source_sha256) is not str
            or _SHA256.fullmatch(self.definition_source_sha256) is None
        ):
            raise R25ArtifactBuildError(
                "INVALID_AUDIT_BINDING", "registry-backed row needs a source digest"
            )
        if self.disposition is CohortTaskAuditDispositionV1.ELIGIBLE:
            if (
                type(self.selection_sha256) is not str
                or _SHA256.fullmatch(self.selection_sha256) is None
            ):
                raise R25ArtifactBuildError(
                    "INVALID_AUDIT_BINDING", "eligible row needs a selection digest"
                )
        elif self.selection_sha256 is not None:
            raise R25ArtifactBuildError(
                "INVALID_AUDIT_BINDING", "excluded row cannot have a selection digest"
            )


@dataclass(frozen=True, slots=True)
class CohortSelectionV1:
    source_path: str
    source_sha256: str
    source_byte_count: int
    registry_sha256: str
    registry_task_count: int
    source_task_count: int
    eligible_task_count: int
    excluded_missing_registry: int
    excluded_user_interaction: int
    excluded_mcp: int
    excluded_dynamic_time: int
    source_task_audit: tuple[CohortTaskAuditRecordV1, ...]
    members: tuple[CohortMemberV1, ...]

    def __post_init__(self) -> None:
        if (
            type(self.source_path) is not str
            or not Path(self.source_path).is_absolute()
            or not self.source_path
            or "\x00" in self.source_path
            or len(self.source_path) > 4096
        ):
            raise R25ArtifactBuildError("INVALID_PATH", "source path must be absolute")
        for value, name in (
            (self.source_sha256, "source_sha256"),
            (self.registry_sha256, "registry_sha256"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise R25ArtifactBuildError("INVALID_DIGEST", f"{name} is invalid")
        counts = (
            self.source_byte_count,
            self.registry_task_count,
            self.source_task_count,
            self.eligible_task_count,
            self.excluded_missing_registry,
            self.excluded_user_interaction,
            self.excluded_mcp,
            self.excluded_dynamic_time,
        )
        if any(type(value) is not int or value < 0 or value > 10_000 for value in counts[1:]):
            raise R25ArtifactBuildError("INVALID_CENSUS", "selection census is invalid")
        if (
            type(self.source_byte_count) is not int
            or not 1 <= self.source_byte_count <= _MAX_SOURCE_BYTES
            or self.registry_task_count < 1
            or not 20 <= self.source_task_count <= _MAX_SOURCE_ROWS
            or self.registry_task_count < self.source_task_count - self.excluded_missing_registry
        ):
            raise R25ArtifactBuildError("INVALID_CENSUS", "selection source is empty")
        if type(self.source_task_audit) is not tuple or any(
            type(record) is not CohortTaskAuditRecordV1 for record in self.source_task_audit
        ):
            raise R25ArtifactBuildError("INVALID_AUDIT_CENSUS", "source audit is untrusted")
        if (
            type(self.members) is not tuple
            or not 20 <= len(self.members) <= 30
            or any(type(member) is not CohortMemberV1 for member in self.members)
        ):
            raise R25ArtifactBuildError("INVALID_COHORT_SIZE", "selected cohort is invalid")
        if len(self.source_task_audit) != self.source_task_count:
            raise R25ArtifactBuildError(
                "INVALID_AUDIT_CENSUS", "source audit does not cover every source row"
            )
        if tuple(record.source_row_index for record in self.source_task_audit) != tuple(
            range(1, self.source_task_count + 1)
        ):
            raise R25ArtifactBuildError(
                "INVALID_AUDIT_CENSUS", "source audit row indices are not exact"
            )
        if len({record.task_id for record in self.source_task_audit}) != self.source_task_count:
            raise R25ArtifactBuildError(
                "INVALID_AUDIT_CENSUS", "source audit repeats a task identity"
            )
        disposition_counts = {
            disposition: sum(record.disposition is disposition for record in self.source_task_audit)
            for disposition in CohortTaskAuditDispositionV1
        }
        expected_counts = {
            CohortTaskAuditDispositionV1.ELIGIBLE: self.eligible_task_count,
            CohortTaskAuditDispositionV1.EXCLUDED_MISSING_REGISTRY: (
                self.excluded_missing_registry
            ),
            CohortTaskAuditDispositionV1.EXCLUDED_USER_INTERACTION: (
                self.excluded_user_interaction
            ),
            CohortTaskAuditDispositionV1.EXCLUDED_MCP: self.excluded_mcp,
            CohortTaskAuditDispositionV1.EXCLUDED_DYNAMIC_OR_UNKNOWN_WALL_CLOCK: (
                self.excluded_dynamic_time
            ),
        }
        if disposition_counts != expected_counts:
            raise R25ArtifactBuildError(
                "INVALID_AUDIT_CENSUS", "source audit counts do not match selection census"
            )
        eligible = {
            record.task_id: record
            for record in self.source_task_audit
            if record.disposition is CohortTaskAuditDispositionV1.ELIGIBLE
        }
        expected_members = tuple(
            record.task_id
            for record in sorted(
                eligible.values(),
                key=lambda record: (
                    cast(str, record.selection_sha256),
                    record.task_id.encode("utf-8"),
                ),
            )[: len(self.members)]
        )
        if tuple(member.task_id for member in self.members) != expected_members:
            raise R25ArtifactBuildError(
                "INVALID_SELECTION", "members are not the deterministic audit prefix"
            )
        for member in self.members:
            record = eligible[member.task_id]
            if member.trial != record.trial or member.selection_sha256 != record.selection_sha256:
                raise R25ArtifactBuildError(
                    "INVALID_SELECTION", "member differs from its audited source row"
                )
            parameters: dict[str, JsonValue] = {
                "task_name": member.task_id,
                "trial": member.trial,
            }
            expected_parameters_sha256 = canonical_sha256(parameters)
            expected_reset_seed = _reset_seed(
                source_sha256=self.source_sha256,
                task_id=member.task_id,
                trial=member.trial,
            )
            if (
                member.task_parameters_sha256 != expected_parameters_sha256
                or member.reset_seed != expected_reset_seed
            ):
                raise R25ArtifactBuildError(
                    "INVALID_SELECTION",
                    "member parameters or reset seed are not deterministically derived",
                )
        for record in eligible.values():
            if record.selection_sha256 != _selection_digest(
                source_sha256=self.source_sha256,
                task_id=record.task_id,
            ):
                raise R25ArtifactBuildError(
                    "INVALID_SELECTION",
                    "eligible audit ranking is not derived from the source digest",
                )


@dataclass(frozen=True, slots=True)
class SnapshotDeclarationV1:
    snapshot_path: str
    snapshot_storage_root: str
    snapshot_tree_sha256: str
    snapshot_total_bytes: int
    snapshot_file_count: int
    actor_endpoint: str
    served_model_id: str


@dataclass(frozen=True, slots=True)
class AuthorityArtifactInputsV1:
    source_task_jsonl: Path
    repository_root: Path
    bundle_directory: Path
    runtime_output_root: Path
    secret_file: Path
    topology_comparison_artifact: Path
    qwen_snapshot: SnapshotDeclarationV1
    mai_snapshot: SnapshotDeclarationV1
    qwen_smoke_fixture: Path
    mai_smoke_fixture: Path
    qwen_smoke_task_id: str
    mai_smoke_task_id: str
    source_commit: str
    cohort_id: str
    run_id: str
    frozen_at_utc: str
    authorization_id: str
    authorized_by: str
    issued_at_utc: str
    expires_at_utc: str
    max_steps_per_cell: int = 8
    per_cell_timeout_seconds: int = 900
    max_total_wall_time_seconds: int = 72_000
    max_total_cost_usd_micros: int = 100_000_000
    smoke_wall_time_seconds: int = 300
    smoke_cost_usd_micros: int = 1_000_000
    resource_preflight_wall_time_seconds: int = 3_600
    openai_timeout_ms: int = 120_000


@dataclass(frozen=True, slots=True)
class AuthorityArtifactBundleV1:
    selection: CohortSelectionV1
    source_task_jsonl_bytes: bytes
    task_source: dict[str, JsonValue]
    pilot_manifest: FrozenPilotManifestV1
    authority_manifest: R24R25RunAuthorityManifestV1
    topology_artifact: R24CpuTopologyArtifactV1


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _absolute_path(path: object, name: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or "\x00" in str(path):
        raise R25ArtifactBuildError("INVALID_PATH", f"{name} must be an absolute path")
    return path


def _repo_external(path: Path, repository_root: Path, name: str) -> Path:
    path = _absolute_path(path, name)
    try:
        resolved = path.resolve(strict=False)
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise R25ArtifactBuildError("INVALID_PATH", f"{name} cannot be resolved") from exc
    if _path_within(resolved, repository):
        raise R25ArtifactBuildError("REPOSITORY_PATH_FORBIDDEN", f"{name} must be repo-external")
    return resolved


def _external_reference_without_io(path: Path, repository_root: Path, name: str) -> Path:
    """Validate a lexical external reference without touching the referenced file."""

    path = _absolute_path(path, name)
    normalized = Path(os.path.abspath(os.fspath(path)))
    repository = Path(os.path.abspath(os.fspath(repository_root)))
    if _path_within(normalized, repository):
        raise R25ArtifactBuildError("REPOSITORY_PATH_FORBIDDEN", f"{name} must be repo-external")
    return normalized


def _strict_json(raw: bytes, name: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise R25ArtifactBuildError("DUPLICATE_JSON_KEY", f"{name} repeats a key")
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise R25ArtifactBuildError("NONFINITE_JSON", f"{name} contains a non-finite number")

    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except R25ArtifactBuildError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise R25ArtifactBuildError("INVALID_JSON", f"{name} is not strict JSON") from exc


def _read_regular_file(path: Path, *, maximum: int, name: str) -> bytes:
    path = _absolute_path(path, name)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise R25ArtifactBuildError("INVALID_FILE", f"{name} must be a regular file")
        if not 1 <= before.st_size <= maximum:
            raise R25ArtifactBuildError("INVALID_FILE_SIZE", f"{name} size is outside bounds")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    except R25ArtifactBuildError:
        raise
    except OSError as exc:
        raise R25ArtifactBuildError("UNREADABLE_FILE", f"{name} cannot be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(raw) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ino != before.st_ino
        or after.st_dev != before.st_dev
    ):
        raise R25ArtifactBuildError("FILE_DRIFT", f"{name} changed while being read")
    return raw


def _registry_projection(records: tuple[RegistryTaskMetadataV1, ...]) -> dict[str, JsonValue]:
    if type(records) is not tuple or not 1 <= len(records) <= _MAX_SOURCE_ROWS:
        raise R25ArtifactBuildError("EMPTY_REGISTRY", "task registry is empty")
    if any(type(record) is not RegistryTaskMetadataV1 for record in records):
        raise R25ArtifactBuildError("INVALID_REGISTRY_METADATA", "registry member is untrusted")
    ordered = tuple(sorted(records, key=lambda item: item.task_id.encode("utf-8")))
    if len({record.task_id for record in ordered}) != len(ordered):
        raise R25ArtifactBuildError("DUPLICATE_REGISTRY_TASK", "task registry repeats an ID")
    return {
        "tasks": [
            {
                "app_names": list(record.app_names),
                "definition_source_sha256": record.definition_source_sha256,
                "task_id": record.task_id,
                "task_tags": list(record.task_tags),
                "task_time_dependency": record.task_time_dependency.value,
            }
            for record in ordered
        ]
    }


def current_registry_metadata() -> tuple[RegistryTaskMetadataV1, ...]:
    """Load only local task definitions and return their selection metadata."""

    from mobile_world.tasks.base import BaseTask
    from mobile_world.tasks.registry import TaskRegistry

    registry = TaskRegistry()
    registered_task_ids = set(registry.list_tasks())
    definition_sources: dict[str, bytes] = {}
    for source_path in sorted(
        Path(registry.task_set_path).rglob("*.py"),
        key=lambda item: str(item).encode("utf-8"),
    ):
        if source_path.name == "__init__.py":
            continue
        source_raw = _read_regular_file(
            source_path.resolve(strict=True),
            maximum=_MAX_TASK_DEFINITION_BYTES,
            name="task definition source",
        )
        try:
            syntax = ast.parse(source_raw, filename=source_path.name)
        except (SyntaxError, ValueError) as exc:
            raise R25ArtifactBuildError(
                "TASK_DEFINITION_SOURCE_INVALID",
                "task definition source cannot be audited",
            ) from exc
        for statement in syntax.body:
            if not isinstance(statement, ast.ClassDef) or statement.name not in registered_task_ids:
                continue
            if statement.name in definition_sources:
                raise R25ArtifactBuildError(
                    "DUPLICATE_TASK_DEFINITION_SOURCE",
                    "task class name appears in more than one source file",
                )
            definition_sources[statement.name] = source_raw
    records: list[RegistryTaskMetadataV1] = []
    for task_id in sorted(registry.list_tasks(), key=lambda item: item.encode("utf-8")):
        task = cast(BaseTask, registry.get_task(task_id))
        raw_tags = task.task_tags
        raw_apps = task.app_names
        if type(raw_tags) is not set or type(raw_apps) is not set:
            raise R25ArtifactBuildError(
                "INVALID_REGISTRY_METADATA", "task tags and app names must be exact sets"
            )
        if any(type(item) is not str for item in raw_tags | raw_apps):
            raise R25ArtifactBuildError(
                "INVALID_REGISTRY_METADATA", "task metadata contains a non-string"
            )
        definition_raw = definition_sources.get(task_id)
        if definition_raw is None:
            raise R25ArtifactBuildError(
                "TASK_DEFINITION_SOURCE_UNAVAILABLE",
                "task definition source is unavailable for time-dependency audit",
            )
        dynamic_time = (
            type(task).initialize_task_hook is BaseTask.initialize_task_hook
            or bool(raw_apps & _DYNAMIC_TIME_APPS)
            or any(marker in definition_raw for marker in _DYNAMIC_TIME_SOURCE_MARKERS)
        )
        records.append(
            RegistryTaskMetadataV1(
                task_id=task_id,
                task_tags=tuple(sorted(raw_tags, key=lambda item: item.encode("utf-8"))),
                app_names=tuple(sorted(raw_apps, key=lambda item: item.encode("utf-8"))),
                task_time_dependency=(
                    RegistryTaskTimeDependencyV1.DYNAMIC_OR_UNKNOWN_WALL_CLOCK
                    if dynamic_time
                    else RegistryTaskTimeDependencyV1.STATIC_WALL_CLOCK_INDEPENDENT
                ),
                definition_source_sha256=hashlib.sha256(definition_raw).hexdigest(),
            )
        )
    return tuple(records)


def _source_rows(raw: bytes) -> tuple[MobileWorldTaskParametersV1, ...]:
    rows: list[MobileWorldTaskParametersV1] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        if not raw_line.strip():
            continue
        if len(rows) >= _MAX_SOURCE_ROWS:
            raise R25ArtifactBuildError("SOURCE_ROW_LIMIT", "task source has too many rows")
        decoded = _strict_json(raw_line, f"task source row {line_number}")
        if type(decoded) is not dict or set(cast(dict[object, object], decoded)) != {
            "task_name",
            "trial",
        }:
            raise R25ArtifactBuildError(
                "INVALID_SOURCE_ROW", "task source rows need exact task_name/trial fields"
            )
        item = cast(dict[str, object], decoded)
        try:
            rows.append(
                MobileWorldTaskParametersV1(
                    task_name=cast(str, item["task_name"]),
                    trial=cast(int, item["trial"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise R25ArtifactBuildError(
                "INVALID_SOURCE_ROW", "task source row values are invalid"
            ) from exc
    if not rows:
        raise R25ArtifactBuildError("EMPTY_SOURCE", "task source has no task rows")
    if len({row.task_name for row in rows}) != len(rows):
        raise R25ArtifactBuildError("DUPLICATE_SOURCE_TASK", "task source repeats a task")
    return tuple(rows)


def _exclusion_reason(record: RegistryTaskMetadataV1) -> str | None:
    folded_name = record.task_id.casefold()
    folded_tags = {item.casefold() for item in record.task_tags}
    folded_apps = {item.casefold() for item in record.app_names}
    if "askuser" in folded_name or "agent-user-interaction" in folded_tags:
        return "USER_INTERACTION"
    if (
        "mcp" in folded_name
        or "agent-mcp" in folded_tags
        or any("mcp" in app for app in folded_apps)
    ):
        return "MCP"
    return None


def _selection_digest(*, source_sha256: str, task_id: str) -> str:
    return hashlib.sha256(
        _SELECTION_DOMAIN + source_sha256.encode("ascii") + b"\0" + task_id.encode("utf-8")
    ).hexdigest()


def _reset_seed(*, source_sha256: str, task_id: str, trial: int) -> int:
    reset_digest = hashlib.sha256(
        _RESET_SEED_DOMAIN
        + source_sha256.encode("ascii")
        + b"\0"
        + task_id.encode("utf-8")
        + b"\0"
        + str(trial).encode("ascii")
    ).digest()
    return int.from_bytes(reset_digest[:8], "big") % 2_147_483_647 + 1


def select_gui_only_cohort_from_bytes(
    source_raw: bytes,
    source_path: Path,
    registry_records: tuple[RegistryTaskMetadataV1, ...],
    *,
    cohort_size: int = 20,
) -> CohortSelectionV1:
    """Recompute one cohort from already authority-read source bytes.

    The production resolver uses this entry point after its own no-symlink,
    trust-root-constrained read so selection never performs a second TOCTOU-
    vulnerable source read.
    """

    if type(source_raw) is not bytes or not 1 <= len(source_raw) <= _MAX_SOURCE_BYTES:
        raise R25ArtifactBuildError("INVALID_FILE_SIZE", "task source size is outside bounds")
    if not isinstance(source_path, Path) or not source_path.is_absolute():  # type: ignore[redundant-expr]
        raise R25ArtifactBuildError("INVALID_PATH", "task source path must be absolute")
    if type(cohort_size) is not int or not 20 <= cohort_size <= 30:
        raise R25ArtifactBuildError("INVALID_COHORT_SIZE", "cohort size must be 20--30")
    source_sha256 = hashlib.sha256(source_raw).hexdigest()
    source_rows = _source_rows(source_raw)
    registry_value = _registry_projection(registry_records)
    registry_sha256 = canonical_sha256(cast(JsonValue, registry_value))
    registry = {record.task_id: record for record in registry_records}

    excluded_missing = excluded_user = excluded_mcp = excluded_dynamic_time = 0
    ranked: list[tuple[str, MobileWorldTaskParametersV1]] = []
    source_task_audit: list[CohortTaskAuditRecordV1] = []
    for source_row_index, row in enumerate(source_rows, start=1):
        record = registry.get(row.task_name)
        if record is None:
            excluded_missing += 1
            disposition = CohortTaskAuditDispositionV1.EXCLUDED_MISSING_REGISTRY
            definition_source_sha256 = None
            selection_sha256 = None
        else:
            reason = _exclusion_reason(record)
            if reason == "USER_INTERACTION":
                excluded_user += 1
                disposition = CohortTaskAuditDispositionV1.EXCLUDED_USER_INTERACTION
                selection_sha256 = None
            elif reason == "MCP":
                excluded_mcp += 1
                disposition = CohortTaskAuditDispositionV1.EXCLUDED_MCP
                selection_sha256 = None
            elif (
                record.task_time_dependency
                is not RegistryTaskTimeDependencyV1.STATIC_WALL_CLOCK_INDEPENDENT
            ):
                excluded_dynamic_time += 1
                disposition = CohortTaskAuditDispositionV1.EXCLUDED_DYNAMIC_OR_UNKNOWN_WALL_CLOCK
                selection_sha256 = None
            else:
                disposition = CohortTaskAuditDispositionV1.ELIGIBLE
                selection_sha256 = _selection_digest(
                    source_sha256=source_sha256,
                    task_id=row.task_name,
                )
                ranked.append((selection_sha256, row))
            definition_source_sha256 = record.definition_source_sha256
        source_task_audit.append(
            CohortTaskAuditRecordV1(
                source_row_index=source_row_index,
                task_id=row.task_name,
                trial=row.trial,
                disposition=disposition,
                definition_source_sha256=definition_source_sha256,
                selection_sha256=selection_sha256,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1].task_name.encode("utf-8")))
    if len(ranked) < cohort_size:
        raise R25ArtifactBuildError(
            "INSUFFICIENT_ELIGIBLE_TASKS", "task source has too few eligible registry tasks"
        )

    members = tuple(
        CohortMemberV1(
            task_id=row.task_name,
            trial=row.trial,
            selection_sha256=selection_sha256,
            reset_seed=_reset_seed(
                source_sha256=source_sha256,
                task_id=row.task_name,
                trial=row.trial,
            ),
            task_parameters_sha256=canonical_sha256(
                cast(
                    JsonValue,
                    {"task_name": row.task_name, "trial": row.trial},
                )
            ),
        )
        for selection_sha256, row in ranked[:cohort_size]
    )
    return CohortSelectionV1(
        source_path=str(source_path),
        source_sha256=source_sha256,
        source_byte_count=len(source_raw),
        registry_sha256=registry_sha256,
        registry_task_count=len(registry_records),
        source_task_count=len(source_rows),
        eligible_task_count=len(ranked),
        excluded_missing_registry=excluded_missing,
        excluded_user_interaction=excluded_user,
        excluded_mcp=excluded_mcp,
        excluded_dynamic_time=excluded_dynamic_time,
        source_task_audit=tuple(source_task_audit),
        members=members,
    )


def select_gui_only_cohort(
    source_path: Path,
    registry_records: tuple[RegistryTaskMetadataV1, ...],
    *,
    cohort_size: int = 20,
) -> CohortSelectionV1:
    """Select a stable cohort from one explicitly supplied GUI-only JSONL source."""

    raw = _read_regular_file(source_path, maximum=_MAX_SOURCE_BYTES, name="task source")
    return select_gui_only_cohort_from_bytes(
        raw,
        source_path,
        registry_records,
        cohort_size=cohort_size,
    )


def verify_current_source_commit(repository_root: Path, source_commit: str) -> None:
    """Optionally bind the explicit commit to local HEAD and a clean worktree."""

    if type(source_commit) is not str or _GIT_SHA1.fullmatch(source_commit) is None:
        raise R25ArtifactBuildError("INVALID_SOURCE_COMMIT", "source commit is not full SHA-1")
    repository = _absolute_path(repository_root, "repository root")
    environment = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    commands = (
        ("HEAD", ["/usr/bin/git", "rev-parse", "HEAD"]),
        (
            "STATUS",
            ["/usr/bin/git", "status", "--porcelain=v1", "--untracked-files=all"],
        ),
    )
    results: dict[str, bytes] = {}
    for label, command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=repository,
                env=environment,
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise R25ArtifactBuildError(
                "GIT_STATE_UNAVAILABLE", "git state is unavailable"
            ) from exc
        if result.returncode != 0 or result.stderr:
            raise R25ArtifactBuildError("GIT_STATE_UNAVAILABLE", "git state is unavailable")
        results[label] = result.stdout
    try:
        head = results["HEAD"].decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise R25ArtifactBuildError("GIT_STATE_UNAVAILABLE", "git HEAD is not ASCII") from exc
    if head != source_commit:
        raise R25ArtifactBuildError("SOURCE_COMMIT_MISMATCH", "source commit differs from HEAD")
    if results["STATUS"]:
        raise R25ArtifactBuildError("DIRTY_WORKTREE", "source worktree is not clean")


def _fixture_binding(path: Path, name: str) -> tuple[str, int]:
    raw = _read_regular_file(path, maximum=_MAX_FIXTURE_BYTES, name=name)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _snapshot_resource(host: PilotHostV1, declaration: SnapshotDeclarationV1) -> SnapshotResourceV1:
    codec = (
        "mobileworld.g1.history-codec.qwen-flat-progress"
        if host is PilotHostV1.QWEN3_VL
        else "mobileworld.g1.history-codec.mai-raw-replay"
    )
    return SnapshotResourceV1(
        host=host,
        history_codec_id=codec,
        snapshot_path=declaration.snapshot_path,
        snapshot_storage_root=declaration.snapshot_storage_root,
        snapshot_tree_algorithm=SNAPSHOT_TREE_ALGORITHM_V1,
        snapshot_tree_sha256=declaration.snapshot_tree_sha256,
        snapshot_total_bytes=declaration.snapshot_total_bytes,
        snapshot_file_count=declaration.snapshot_file_count,
        actor_endpoint=declaration.actor_endpoint,
        served_model_id=declaration.served_model_id,
        host_enabled=True,
        independent_kill_switch=True,
    )


def _smoke_plan(
    host: PilotHostV1,
    fixture_path: Path,
    fixture_sha256: str,
    fixture_byte_count: int,
    task_id: str,
    *,
    wall_time_seconds: int,
    cost_usd_micros: int,
) -> HostLiveSmokePlanV1:
    cases = tuple(
        LiveSmokeCaseV1(
            case_id=f"{host.value.lower()}-{mode.value.lower()}",
            task_id=task_id,
            mode=mode,
            request_fixture_path=str(fixture_path),
            request_fixture_sha256=fixture_sha256,
            request_fixture_byte_count=fixture_byte_count,
            max_actor_calls=1,
            max_openai_calls=0 if mode is SmokeModeV1.OFF else 3,
            max_wall_time_seconds=wall_time_seconds,
            max_cost_usd_micros=cost_usd_micros,
            actor_action_allowed=False,
            provider_final_request_proof_required=True,
        )
        for mode in SmokeModeV1
    )
    return HostLiveSmokePlanV1(host=host, cases=cases)


def build_authority_artifact_bundle(
    inputs: AuthorityArtifactInputsV1,
    registry_records: tuple[RegistryTaskMetadataV1, ...],
) -> AuthorityArtifactBundleV1:
    """Construct complete canonical projections without performing live work."""

    repository_root = _absolute_path(inputs.repository_root, "repository root").resolve(strict=True)
    if not repository_root.is_dir():
        raise R25ArtifactBuildError("INVALID_REPOSITORY", "repository root is not a directory")
    bundle_directory = _repo_external(inputs.bundle_directory, repository_root, "bundle directory")
    runtime_output_root = _repo_external(
        inputs.runtime_output_root, repository_root, "runtime output root"
    )
    secret_file = _external_reference_without_io(inputs.secret_file, repository_root, "secret file")
    topology_raw = _read_regular_file(
        inputs.topology_comparison_artifact,
        maximum=_MAX_TOPOLOGY_ARTIFACT_BYTES,
        name="CPU topology comparison artifact",
    )
    topology_value = _strict_json(topology_raw, "CPU topology comparison artifact")
    try:
        topology_artifact = parse_r24_cpu_topology_artifact(topology_value)
    except ValueError as exc:
        raise R25ArtifactBuildError(
            "INVALID_TOPOLOGY_COMPARISON",
            "CPU topology comparison artifact failed closed validation",
        ) from exc
    topology_projection = cast(JsonValue, r24_cpu_topology_artifact_projection(topology_artifact))
    topology_bytes = canonical_json_bytes(topology_projection)
    if topology_raw != topology_bytes:
        raise R25ArtifactBuildError(
            "NONCANONICAL_TOPOLOGY_COMPARISON",
            "CPU topology comparison artifact is not exact canonical JSON",
        )
    topology_sha256 = r24_cpu_topology_artifact_sha256(topology_artifact)
    if type(inputs.source_commit) is not str or _GIT_SHA1.fullmatch(inputs.source_commit) is None:
        raise R25ArtifactBuildError("INVALID_SOURCE_COMMIT", "source commit is not full SHA-1")
    for path, name in (
        (bundle_directory, "bundle directory"),
        (runtime_output_root, "runtime output root"),
    ):
        if path.exists() or path.is_symlink():
            raise R25ArtifactBuildError("OUTPUT_NOT_FRESH", f"{name} must not exist")
        try:
            parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise R25ArtifactBuildError("INVALID_PATH", f"{name} parent does not exist") from exc
        if not parent.is_dir():
            raise R25ArtifactBuildError("INVALID_PATH", f"{name} parent is not a directory")

    source_task_jsonl_bytes = _read_regular_file(
        inputs.source_task_jsonl,
        maximum=_MAX_SOURCE_BYTES,
        name="task source",
    )
    bundled_source_path = bundle_directory / GUI_ONLY_TASK_SOURCE_FILENAME
    selection = select_gui_only_cohort_from_bytes(
        source_task_jsonl_bytes,
        bundled_source_path,
        registry_records,
    )
    selection_bytes = canonical_json_bytes(cast(JsonValue, cohort_selection_projection(selection)))
    selection_sha256 = hashlib.sha256(selection_bytes).hexdigest()
    registry_by_id = {record.task_id: record for record in registry_records}
    for task_id in (inputs.qwen_smoke_task_id, inputs.mai_smoke_task_id):
        record = registry_by_id.get(task_id)
        if record is None or _exclusion_reason(record) is not None:
            raise R25ArtifactBuildError(
                "INVALID_SMOKE_TASK", "smoke task must be a current GUI-only registry task"
            )
    pilot_tasks = tuple(
        PilotTaskV1(
            task_id=member.task_id,
            task_parameters_sha256=member.task_parameters_sha256,
            reset_seed=member.reset_seed,
        )
        for member in selection.members
    )
    parameter_bindings = tuple(
        InlinePilotTaskParametersV1(
            task_id=member.task_id,
            parameters=MobileWorldTaskParametersV1(
                task_name=member.task_id,
                trial=member.trial,
            ),
        )
        for member in selection.members
    )
    task_source = executable_pilot_task_source_projection(
        inputs.cohort_id,
        pilot_tasks,
        parameter_bindings,
    )
    task_source_bytes = canonical_json_bytes(cast(JsonValue, task_source))
    task_source_path = bundle_directory / PILOT_TASK_SOURCE_FILENAME
    cell_count = len(pilot_tasks) * 2 * 2
    max_actor_calls = cell_count * inputs.max_steps_per_cell
    joint_cell_count = len(pilot_tasks) * 2
    max_openai_calls = joint_cell_count + 2 * joint_cell_count * inputs.max_steps_per_cell
    pilot = FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id=inputs.cohort_id,
        frozen_at_utc=inputs.frozen_at_utc,
        task_manifest_path=str(task_source_path),
        task_manifest_sha256=hashlib.sha256(task_source_bytes).hexdigest(),
        task_manifest_byte_count=len(task_source_bytes),
        topology_comparison_artifact_path=str(bundle_directory / TOPOLOGY_COMPARISON_FILENAME),
        topology_comparison_artifact_sha256=topology_sha256,
        topology_comparison_artifact_byte_count=len(topology_bytes),
        cohort_selection_artifact_path=str(bundle_directory / COHORT_SELECTION_FILENAME),
        cohort_selection_artifact_sha256=selection_sha256,
        cohort_selection_artifact_byte_count=len(selection_bytes),
        cohort_selection_sha256=selection_sha256,
        task_time_authority=(PilotTaskTimeAuthorityV1.STATIC_WALL_CLOCK_INDEPENDENT_ONLY),
        dynamic_wall_clock_tasks_excluded=True,
        tasks=pilot_tasks,
        hosts=(PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI),
        arms=(PilotArmV1.BASELINE, PilotArmV1.JOINT_SENTINEL),
        topology=PilotTopologyV1.ISOLATED_HISTORY_FREE,
        seed_policy=PilotSeedPolicyV1.FIXED_PER_TASK_SHARED_ACROSS_HOSTS_AND_ARMS,
        baseline_mode="OFF",
        joint_mode="ACTIVE",
        environment_reset_between_cells=True,
        matched_task_ids=True,
        matched_task_parameters=True,
        official_success_metric_required=True,
        max_steps_per_cell=inputs.max_steps_per_cell,
        per_cell_timeout_seconds=inputs.per_cell_timeout_seconds,
        max_total_wall_time_seconds=inputs.max_total_wall_time_seconds,
        max_total_actor_calls=max_actor_calls,
        max_total_openai_calls=max_openai_calls,
        max_total_cost_usd_micros=inputs.max_total_cost_usd_micros,
    )
    qwen_fixture_sha256, qwen_fixture_bytes = _fixture_binding(
        inputs.qwen_smoke_fixture, "Qwen smoke fixture"
    )
    mai_fixture_sha256, mai_fixture_bytes = _fixture_binding(
        inputs.mai_smoke_fixture, "MAI smoke fixture"
    )
    smokes = (
        _smoke_plan(
            PilotHostV1.QWEN3_VL,
            inputs.qwen_smoke_fixture,
            qwen_fixture_sha256,
            qwen_fixture_bytes,
            inputs.qwen_smoke_task_id,
            wall_time_seconds=inputs.smoke_wall_time_seconds,
            cost_usd_micros=inputs.smoke_cost_usd_micros,
        ),
        _smoke_plan(
            PilotHostV1.MAI_UI,
            inputs.mai_smoke_fixture,
            mai_fixture_sha256,
            mai_fixture_bytes,
            inputs.mai_smoke_task_id,
            wall_time_seconds=inputs.smoke_wall_time_seconds,
            cost_usd_micros=inputs.smoke_cost_usd_micros,
        ),
    )
    smoke_actor_calls = sum(case.max_actor_calls for plan in smokes for case in plan.cases)
    smoke_openai_calls = sum(case.max_openai_calls for plan in smokes for case in plan.cases)
    smoke_cost = sum(case.max_cost_usd_micros for plan in smokes for case in plan.cases)
    smoke_wall_time = sum(case.max_wall_time_seconds for plan in smokes for case in plan.cases)
    authority = R24R25RunAuthorityManifestV1(
        schema_version=R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
        run_id=inputs.run_id,
        source_commit=inputs.source_commit,
        authorization=OwnerAuthorizationV1(
            status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED,
            authorization_id=inputs.authorization_id,
            authorized_by=inputs.authorized_by,
            issued_at_utc=inputs.issued_at_utc,
            expires_at_utc=inputs.expires_at_utc,
            network_allowed=True,
            gpu_allowed=True,
            docker_allowed=True,
            model_loading_allowed=True,
            backend_allowed=True,
            actor_model_calls_allowed=True,
            sentinel_provider_calls_allowed=True,
            pilot_gui_actions_allowed=True,
            smoke_gui_actions_allowed=False,
            merge_allowed=False,
            linear_update_allowed=False,
            frozen_artifact_mutation_allowed=False,
        ),
        safety=SequenceSafetyV1(
            stages=(
                RunStageV1.RESOURCE_PREFLIGHT,
                RunStageV1.QWEN_LIVE_SMOKE,
                RunStageV1.MAI_LIVE_SMOKE,
                RunStageV1.R25_PILOT,
            ),
            stop_on_failure=True,
            pilot_only_after_both_smokes_pass=True,
            default_dry_run=True,
            arbitrary_commands_forbidden=True,
            secrets_in_logs_forbidden=True,
            repo_external_output_required=True,
        ),
        secret=SecretFileReferenceV1(
            path=str(secret_file),
            environment_key="OPENAI_API_KEY",
            required_mode=0o600,
            content_may_be_read_by_preflight=False,
            persist_value_or_hash=False,
        ),
        openai_stages=(
            OpenAIResponsesStageV1(
                role=OpenAIRoleV1.RUBRIC,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=8192,
                timeout_ms=inputs.openai_timeout_ms,
                max_attempts=1,
                store=False,
            ),
            OpenAIResponsesStageV1(
                role=OpenAIRoleV1.HISTORY_POLICY,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=4096,
                timeout_ms=inputs.openai_timeout_ms,
                max_attempts=1,
                store=False,
            ),
        ),
        actor_resources=(
            _snapshot_resource(PilotHostV1.QWEN3_VL, inputs.qwen_snapshot),
            _snapshot_resource(PilotHostV1.MAI_UI, inputs.mai_snapshot),
        ),
        smoke_plans=smokes,
        pilot=pilot,
        topology_comparison_artifact_sha256=topology_sha256,
        output_root=str(runtime_output_root),
        max_resource_preflight_wall_time_seconds=inputs.resource_preflight_wall_time_seconds,
        max_sequence_wall_time_seconds=(
            inputs.resource_preflight_wall_time_seconds
            + smoke_wall_time
            + inputs.max_total_wall_time_seconds
        ),
        max_sequence_openai_calls=smoke_openai_calls + pilot.max_total_openai_calls,
        max_sequence_actor_calls=smoke_actor_calls + pilot.max_total_actor_calls,
        max_sequence_cost_usd_micros=smoke_cost + pilot.max_total_cost_usd_micros,
    )
    return AuthorityArtifactBundleV1(
        selection=selection,
        source_task_jsonl_bytes=source_task_jsonl_bytes,
        task_source=task_source,
        pilot_manifest=pilot,
        authority_manifest=authority,
        topology_artifact=topology_artifact,
    )


def cohort_selection_projection(selection: CohortSelectionV1) -> dict[str, JsonValue]:
    if type(selection) is not CohortSelectionV1:
        raise R25ArtifactBuildError("UNTRUSTED_SELECTION", "selection type is untrusted")
    return {
        "algorithm": COHORT_SELECTION_ALGORITHM,
        "eligible_task_count": selection.eligible_task_count,
        "excluded_mcp": selection.excluded_mcp,
        "excluded_dynamic_time": selection.excluded_dynamic_time,
        "excluded_missing_registry": selection.excluded_missing_registry,
        "excluded_user_interaction": selection.excluded_user_interaction,
        "members": [
            {
                "reset_seed": member.reset_seed,
                "selection_sha256": member.selection_sha256,
                "task_id": member.task_id,
                "task_parameters_sha256": member.task_parameters_sha256,
                "trial": member.trial,
            }
            for member in selection.members
        ],
        "registry_sha256": selection.registry_sha256,
        "registry_task_count": selection.registry_task_count,
        "source_byte_count": selection.source_byte_count,
        "source_path": selection.source_path,
        "source_sha256": selection.source_sha256,
        "source_task_count": selection.source_task_count,
        "source_task_audit": [
            {
                "definition_source_sha256": record.definition_source_sha256,
                "disposition": record.disposition.value,
                "selection_sha256": record.selection_sha256,
                "source_row_index": record.source_row_index,
                "task_id": record.task_id,
                "trial": record.trial,
            }
            for record in selection.source_task_audit
        ],
        "task_time_dependency_audit_algorithm": TASK_TIME_DEPENDENCY_AUDIT_ALGORITHM,
        "schema_version": COHORT_SELECTION_SCHEMA_VERSION,
    }


def cohort_selection_sha256(selection: CohortSelectionV1) -> str:
    return canonical_sha256(cast(JsonValue, cohort_selection_projection(selection)))


_COHORT_SELECTION_FIELDS = frozenset(
    {
        "algorithm",
        "eligible_task_count",
        "excluded_dynamic_time",
        "excluded_mcp",
        "excluded_missing_registry",
        "excluded_user_interaction",
        "members",
        "registry_sha256",
        "registry_task_count",
        "schema_version",
        "source_byte_count",
        "source_path",
        "source_sha256",
        "source_task_audit",
        "source_task_count",
        "task_time_dependency_audit_algorithm",
    }
)
_COHORT_MEMBER_FIELDS = frozenset(
    {"reset_seed", "selection_sha256", "task_id", "task_parameters_sha256", "trial"}
)
_COHORT_AUDIT_FIELDS = frozenset(
    {
        "definition_source_sha256",
        "disposition",
        "selection_sha256",
        "source_row_index",
        "task_id",
        "trial",
    }
)


def _exact_selection_object(
    value: object,
    fields: frozenset[str],
    name: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise R25ArtifactBuildError("INVALID_SELECTION_ARTIFACT", f"{name} must be an object")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != fields:
        raise R25ArtifactBuildError("INVALID_SELECTION_ARTIFACT", f"{name} fields are not exact")
    return cast(dict[str, object], mapping)


def parse_cohort_selection(value: object) -> CohortSelectionV1:
    """Strictly parse one independent selection artifact.

    This reconstructs all exact member/audit value types and rechecks every
    hash derivable from the source digest.  The production resolver separately
    rereads the bound source bytes and recomputes current registry metadata.
    """

    item = _exact_selection_object(value, _COHORT_SELECTION_FIELDS, "cohort selection")
    if (
        item["schema_version"] != COHORT_SELECTION_SCHEMA_VERSION
        or item["algorithm"] != COHORT_SELECTION_ALGORITHM
        or item["task_time_dependency_audit_algorithm"] != TASK_TIME_DEPENDENCY_AUDIT_ALGORITHM
    ):
        raise R25ArtifactBuildError(
            "UNKNOWN_SELECTION_ALGORITHM", "selection algorithm/schema is not supported"
        )
    raw_members = item["members"]
    raw_audit = item["source_task_audit"]
    if type(raw_members) is not list or type(raw_audit) is not list:
        raise R25ArtifactBuildError(
            "INVALID_SELECTION_ARTIFACT", "selection collections must be arrays"
        )
    try:
        members = tuple(
            CohortMemberV1(
                task_id=cast(str, member["task_id"]),
                trial=cast(int, member["trial"]),
                selection_sha256=cast(str, member["selection_sha256"]),
                reset_seed=cast(int, member["reset_seed"]),
                task_parameters_sha256=cast(str, member["task_parameters_sha256"]),
            )
            for member in (
                _exact_selection_object(raw, _COHORT_MEMBER_FIELDS, "cohort member")
                for raw in cast(list[object], raw_members)
            )
        )
        audit = tuple(
            CohortTaskAuditRecordV1(
                source_row_index=cast(int, record["source_row_index"]),
                task_id=cast(str, record["task_id"]),
                trial=cast(int, record["trial"]),
                disposition=CohortTaskAuditDispositionV1(cast(str, record["disposition"])),
                definition_source_sha256=cast(str | None, record["definition_source_sha256"]),
                selection_sha256=cast(str | None, record["selection_sha256"]),
            )
            for record in (
                _exact_selection_object(raw, _COHORT_AUDIT_FIELDS, "cohort audit record")
                for raw in cast(list[object], raw_audit)
            )
        )
        selection = CohortSelectionV1(
            source_path=cast(str, item["source_path"]),
            source_sha256=cast(str, item["source_sha256"]),
            source_byte_count=cast(int, item["source_byte_count"]),
            registry_sha256=cast(str, item["registry_sha256"]),
            registry_task_count=cast(int, item["registry_task_count"]),
            source_task_count=cast(int, item["source_task_count"]),
            eligible_task_count=cast(int, item["eligible_task_count"]),
            excluded_missing_registry=cast(int, item["excluded_missing_registry"]),
            excluded_user_interaction=cast(int, item["excluded_user_interaction"]),
            excluded_mcp=cast(int, item["excluded_mcp"]),
            excluded_dynamic_time=cast(int, item["excluded_dynamic_time"]),
            source_task_audit=audit,
            members=members,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, R25ArtifactBuildError):
            raise
        raise R25ArtifactBuildError(
            "INVALID_SELECTION_ARTIFACT", "selection values are invalid"
        ) from exc
    if cohort_selection_projection(selection) != value:
        raise R25ArtifactBuildError(
            "NONCANONICAL_SELECTION", "selection projection differs after strict reconstruction"
        )
    return selection


def _trusted_task_source(bundle: AuthorityArtifactBundleV1) -> dict[str, JsonValue]:
    pilot_tasks = tuple(
        PilotTaskV1(
            task_id=member.task_id,
            task_parameters_sha256=member.task_parameters_sha256,
            reset_seed=member.reset_seed,
        )
        for member in bundle.selection.members
    )
    bindings = tuple(
        InlinePilotTaskParametersV1(
            task_id=member.task_id,
            parameters=MobileWorldTaskParametersV1(
                task_name=member.task_id,
                trial=member.trial,
            ),
        )
        for member in bundle.selection.members
    )
    rebuilt = executable_pilot_task_source_projection(
        bundle.pilot_manifest.cohort_id,
        pilot_tasks,
        bindings,
    )
    if canonical_json_bytes(cast(JsonValue, rebuilt)) != canonical_json_bytes(
        cast(JsonValue, bundle.task_source)
    ):
        raise R25ArtifactBuildError("TASK_SOURCE_MUTATED", "task source changed after build")
    return rebuilt


def artifact_bundle_projection(bundle: AuthorityArtifactBundleV1) -> dict[str, JsonValue]:
    if type(bundle.source_task_jsonl_bytes) is not bytes:
        raise R25ArtifactBuildError("UNTRUSTED_SOURCE_BYTES", "source JSONL bytes are untrusted")
    task_source = cast(JsonValue, _trusted_task_source(bundle))
    pilot_projection = cast(JsonValue, frozen_pilot_manifest_projection(bundle.pilot_manifest))
    authority_projection = cast(JsonValue, authority_manifest_projection(bundle.authority_manifest))
    topology_projection = cast(
        JsonValue, r24_cpu_topology_artifact_projection(bundle.topology_artifact)
    )
    topology_sha256 = r24_cpu_topology_artifact_sha256(bundle.topology_artifact)
    selection_projection = cast(JsonValue, cohort_selection_projection(bundle.selection))
    selection_bytes = canonical_json_bytes(selection_projection)
    selection_sha256 = hashlib.sha256(selection_bytes).hexdigest()
    source_sha256 = hashlib.sha256(bundle.source_task_jsonl_bytes).hexdigest()
    if (
        bundle.pilot_manifest.topology_comparison_artifact_sha256 != topology_sha256
        or bundle.authority_manifest.topology_comparison_artifact_sha256 != topology_sha256
    ):
        raise R25ArtifactBuildError(
            "TOPOLOGY_BINDING_MISMATCH",
            "bundle manifests do not bind the exact CPU topology artifact",
        )
    if (
        source_sha256 != bundle.selection.source_sha256
        or len(bundle.source_task_jsonl_bytes) != bundle.selection.source_byte_count
        or bundle.pilot_manifest.cohort_selection_artifact_sha256 != selection_sha256
        or bundle.pilot_manifest.cohort_selection_artifact_byte_count != len(selection_bytes)
        or bundle.pilot_manifest.cohort_selection_sha256 != selection_sha256
    ):
        raise R25ArtifactBuildError(
            "COHORT_SELECTION_BINDING_MISMATCH",
            "bundle source/selection bytes differ from the frozen pilot bindings",
        )
    return {
        "authority_manifest": authority_projection,
        "authority_manifest_sha256": authority_manifest_sha256(bundle.authority_manifest),
        "authority_status": RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED.value,
        "cohort_selection": selection_projection,
        "cohort_selection_artifact_byte_count": len(selection_bytes),
        "cohort_selection_artifact_sha256": selection_sha256,
        "execution_census": {
            "actor_model_calls": 0,
            "backend_operations": 0,
            "docker_operations": 0,
            "gpu_operations": 0,
            "gui_actions": 0,
            "network_calls": 0,
            "secret_content_reads": 0,
        },
        "executable_task_source": task_source,
        "executable_task_source_byte_count": len(canonical_json_bytes(task_source)),
        "executable_task_source_schema_version": (EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION),
        "executable_task_source_sha256": canonical_sha256(task_source),
        "frozen_pilot_manifest": pilot_projection,
        "frozen_pilot_manifest_sha256": frozen_pilot_manifest_sha256(bundle.pilot_manifest),
        "gui_only_task_source_byte_count": len(bundle.source_task_jsonl_bytes),
        "gui_only_task_source_sha256": source_sha256,
        "schema_version": ARTIFACT_BUNDLE_SCHEMA_VERSION,
        "topology_comparison_artifact": topology_projection,
        "topology_comparison_artifact_byte_count": len(canonical_json_bytes(topology_projection)),
        "topology_comparison_artifact_sha256": topology_sha256,
    }


def artifact_bundle_sha256(bundle: AuthorityArtifactBundleV1) -> str:
    return canonical_sha256(cast(JsonValue, artifact_bundle_projection(bundle)))


def artifact_bundle_output(bundle: AuthorityArtifactBundleV1) -> dict[str, JsonValue]:
    projection = cast(JsonValue, artifact_bundle_projection(bundle))
    return {
        "artifact_bundle": projection,
        "artifact_bundle_sha256": canonical_sha256(projection),
    }


def write_artifact_bundle(
    bundle: AuthorityArtifactBundleV1,
    *,
    repository_root: Path,
) -> tuple[Path, ...]:
    """Write once into the explicitly planned fresh, repo-external directory."""

    target = _repo_external(
        Path(bundle.pilot_manifest.task_manifest_path).parent,
        repository_root,
        "bundle directory",
    )
    try:
        target.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError as exc:
        raise R25ArtifactBuildError(
            "OUTPUT_DIRECTORY_NOT_FRESH", "bundle directory must be a fresh direct child"
        ) from exc
    payloads: tuple[tuple[str, bytes], ...] = (
        (GUI_ONLY_TASK_SOURCE_FILENAME, bundle.source_task_jsonl_bytes),
        (
            COHORT_SELECTION_FILENAME,
            canonical_json_bytes(cast(JsonValue, cohort_selection_projection(bundle.selection))),
        ),
        (
            PILOT_TASK_SOURCE_FILENAME,
            canonical_json_bytes(cast(JsonValue, _trusted_task_source(bundle))),
        ),
        (
            FROZEN_PILOT_MANIFEST_FILENAME,
            canonical_json_bytes(
                cast(JsonValue, frozen_pilot_manifest_projection(bundle.pilot_manifest))
            ),
        ),
        (
            RUN_AUTHORITY_MANIFEST_FILENAME,
            canonical_json_bytes(
                cast(JsonValue, authority_manifest_projection(bundle.authority_manifest))
            ),
        ),
        (
            TOPOLOGY_COMPARISON_FILENAME,
            canonical_json_bytes(
                cast(
                    JsonValue,
                    r24_cpu_topology_artifact_projection(bundle.topology_artifact),
                )
            ),
        ),
        (
            ARTIFACT_BUNDLE_FILENAME,
            canonical_json_bytes(cast(JsonValue, artifact_bundle_output(bundle))),
        ),
    )
    written: list[Path] = []
    try:
        for filename, payload in payloads:
            path = target / filename
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    descriptor = -1
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            written.append(path)
    except OSError as exc:
        raise R25ArtifactBuildError("ARTIFACT_WRITE_FAILED", "artifact write failed") from exc
    return tuple(written)


__all__ = [
    "ARTIFACT_BUNDLE_FILENAME",
    "ARTIFACT_BUNDLE_SCHEMA_VERSION",
    "COHORT_SELECTION_ALGORITHM",
    "COHORT_SELECTION_FILENAME",
    "COHORT_SELECTION_SCHEMA_VERSION",
    "CohortTaskAuditDispositionV1",
    "CohortTaskAuditRecordV1",
    "TASK_TIME_DEPENDENCY_AUDIT_ALGORITHM",
    "FROZEN_PILOT_MANIFEST_FILENAME",
    "GUI_ONLY_TASK_SOURCE_FILENAME",
    "PILOT_TASK_SOURCE_FILENAME",
    "RUN_AUTHORITY_MANIFEST_FILENAME",
    "TOPOLOGY_COMPARISON_FILENAME",
    "AuthorityArtifactBundleV1",
    "AuthorityArtifactInputsV1",
    "CohortMemberV1",
    "CohortSelectionV1",
    "R25ArtifactBuildError",
    "RegistryTaskMetadataV1",
    "RegistryTaskTimeDependencyV1",
    "SnapshotDeclarationV1",
    "artifact_bundle_output",
    "artifact_bundle_projection",
    "artifact_bundle_sha256",
    "build_authority_artifact_bundle",
    "cohort_selection_projection",
    "cohort_selection_sha256",
    "current_registry_metadata",
    "parse_cohort_selection",
    "select_gui_only_cohort",
    "select_gui_only_cohort_from_bytes",
    "verify_current_source_commit",
    "write_artifact_bundle",
]
