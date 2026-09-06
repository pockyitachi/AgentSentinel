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
import inspect
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import cast

from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    RefResolver,
    SchemaError,
    ValidationError,
)

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes, canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V1,
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V2,
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
    parse_authority_manifest,
    production_sentinel_config_sha256_v1,
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
    FROZEN_PILOT_SCHEMA_VERSION_V1,
    FROZEN_PILOT_SCHEMA_VERSION_V2,
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
    parse_frozen_pilot_manifest,
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
GUI_ONLY_TASK_SOURCE_TRIAL = 1
GUI_ONLY_TASK_SOURCE_TASK_COUNT = 117
GUI_ONLY_TASK_SOURCE_TRIAL_PROVENANCE = "NEW_R25_DECLARATION_NOT_HISTORICAL_DERIVATION"
GUI_ONLY_TASK_SOURCE_FREEZE_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.5-gui-only-task-source-freeze/v1"
)

_SELECTION_DOMAIN = b"r25-pilot-v1\0"
_RESET_SEED_DOMAIN = b"r25-reset-seed-v1\0"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}")
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_ROWS = 10_000
_MAX_FIXTURE_BYTES = 100_000_000
_MAX_TOPOLOGY_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_TASK_DEFINITION_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_CURATED_TASK_SET_SCHEMA_VERSION = "mobileworld.audit.curated-task-set/v1"
_CURATED_TASK_SET_ARTIFACT_TYPE = "derived_task_selection"
_CURATED_TASK_SET_FIELDS = frozenset(
    {
        "artifact_type",
        "canonical_catalog",
        "counts",
        "dataset_id",
        "is_raw_run",
        "raw_schema_version",
        "schema_version",
        "selection_policy",
        "selection_sha256",
        "source_locator",
        "sources",
        "tasks",
    }
)
_CURATED_CATALOG_FIELDS = frozenset({"task_catalog_sha256", "task_count", "task_name_index_sha256"})
_CURATED_COUNT_FIELDS = frozenset(
    {
        "blob_reference_occurrences",
        "selected_task_stream_byte_count",
        "task_count",
        "task_count_by_source",
        "unique_blob_byte_count_summed_by_source",
        "unique_blob_count_summed_by_source",
    }
)
_CURATED_SELECTION_POLICY_FIELDS = frozenset(
    {
        "candidate_resolution",
        "canonical_catalog_source_id",
        "collector_error_event_ids_must_be_empty",
        "missing_artifacts_must_be_empty",
        "raw_events_or_blobs_copied",
        "source_run_global_capture_complete_required",
        "task_capture_complete",
        "task_goal_sha256_must_match_catalog",
        "task_outcome_score_filter",
        "task_runtime_status",
        "unit",
    }
)
_CURATED_TASK_FIELDS = frozenset(
    {
        "canonical_suite_index",
        "capture_complete",
        "collector_error_event_ids",
        "environment_evaluation",
        "missing_artifacts",
        "runtime_status",
        "source_id",
        "source_run_id",
        "source_task_index",
        "source_task_run_id",
        "task_ended_event_id",
        "task_goal_utf8_byte_count",
        "task_goal_utf8_sha256",
        "task_name",
        "task_started_event_id",
        "task_stream",
        "whole_task_attempt_index",
    }
)
_WRITTEN_ARTIFACT_FILENAMES = frozenset(
    {
        GUI_ONLY_TASK_SOURCE_FILENAME,
        COHORT_SELECTION_FILENAME,
        PILOT_TASK_SOURCE_FILENAME,
        FROZEN_PILOT_MANIFEST_FILENAME,
        RUN_AUTHORITY_MANIFEST_FILENAME,
        ARTIFACT_BUNDLE_FILENAME,
        TOPOLOGY_COMPARISON_FILENAME,
    }
)
_SCHEMA_RELATIVE_PATHS = (
    "mobileworld_audit_handoff/schemas/r2_4/topology_comparison.v1.schema.json",
    "mobileworld_audit_handoff/schemas/r2_4/cpu_topology_artifact.v1.schema.json",
    "mobileworld_audit_handoff/schemas/r2_4/run_authority_manifest.v1.schema.json",
    "mobileworld_audit_handoff/schemas/r2_4/run_authority_manifest.v2.schema.json",
    "mobileworld_audit_handoff/schemas/r2_5/frozen_pilot_manifest.v1.schema.json",
    "mobileworld_audit_handoff/schemas/r2_5/frozen_pilot_manifest.v2.schema.json",
    "mobileworld_audit_handoff/schemas/r2_5/cohort_selection.v1.schema.json",
    "mobileworld_audit_handoff/schemas/r2_5/executable_task_source.v1.schema.json",
    "mobileworld_audit_handoff/schemas/r2_5/artifact_bundle.v1.schema.json",
)
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
_DYNAMIC_TIME_CALLS = frozenset(
    {
        "datetime.date.today",
        "datetime.datetime.now",
        "datetime.datetime.today",
        "datetime.datetime.utcnow",
        "time.time",
    }
)


def _source_ast_uses_dynamic_wall_clock(syntax: ast.AST) -> bool:
    """Recognize wall-clock calls through ordinary import/assignment aliases.

    This is deliberately conservative: wildcard imports from ``time`` or
    ``datetime`` cannot be resolved statically and therefore classify the
    containing definition source as dynamic/unknown.
    """

    aliases: dict[str, str] = {}
    assignments: list[tuple[str, ast.expr]] = []
    for node in ast.walk(syntax):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in {"time", "datetime"}:
                    aliases[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"time", "datetime"}:
            for imported in node.names:
                if imported.name == "*":
                    return True
                local = imported.asname or imported.name
                aliases[local] = f"{node.module}.{imported.name}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.expr):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assignments.extend(
                (target.id, node.value) for target in targets if isinstance(target, ast.Name)
            )

    def qualified(value: ast.expr) -> str | None:
        if isinstance(value, ast.Name):
            return aliases.get(value.id, value.id)
        if isinstance(value, ast.Attribute):
            parent = qualified(value.value)
            return None if parent is None else f"{parent}.{value.attr}"
        return None

    # Resolve simple ``clock = time_module.time`` aliases without executing
    # task code. Two passes cover the ordinary chained-alias form while
    # remaining bounded and deterministic.
    for _ in range(2):
        changed = False
        for target, expression in assignments:
            resolved = qualified(expression)
            if resolved is not None and aliases.get(target) != resolved:
                aliases[target] = resolved
                changed = True
        if not changed:
            break
    return any(
        isinstance(node, ast.Call) and qualified(node.func) in _DYNAMIC_TIME_CALLS
        for node in ast.walk(syntax)
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
    resource_topology: str
    runtime_config_sha256: str
    pricing_sha256: str
    max_resource_cleanup_wall_time_seconds: int
    resource_cleanup_upper_bound_sha256: str
    max_model_switch_wall_time_seconds: int
    max_post_run_integrity_wall_time_seconds: int
    cohort_size: int = 20
    max_steps_per_cell: int = 8
    per_cell_timeout_seconds: int = 900
    max_total_wall_time_seconds: int = 72_000
    max_total_cost_usd_micros: int = 100_000_000
    smoke_wall_time_seconds: int = 300
    smoke_cost_usd_micros: int = 1_000_000
    resource_preflight_wall_time_seconds: int = 3_600
    openai_timeout_ms: int = 120_000
    source_freeze_receipt: Path | None = None


@dataclass(frozen=True, slots=True)
class AuthorityArtifactBundleV1:
    selection: CohortSelectionV1
    source_task_jsonl_bytes: bytes
    task_source: dict[str, JsonValue]
    pilot_manifest: FrozenPilotManifestV1
    authority_manifest: R24R25RunAuthorityManifestV1
    topology_artifact: R24CpuTopologyArtifactV1
    source_freeze: FrozenGuiOnlyTaskSourceV1


@dataclass(frozen=True, slots=True)
class FrozenGuiOnlyTaskSourceV1:
    """Exact task-name extraction plus a new, explicit R2.5 trial declaration."""

    historical_manifest_path: str
    historical_manifest_sha256: str
    historical_manifest_byte_count: int
    task_source_bytes: bytes
    task_source_sha256: str
    task_count: int
    trial: int
    trial_provenance: str

    def __post_init__(self) -> None:
        if (
            type(self.historical_manifest_path) is not str
            or not Path(self.historical_manifest_path).is_absolute()
        ):
            raise R25ArtifactBuildError(
                "INVALID_HISTORICAL_MANIFEST", "historical manifest path is invalid"
            )
        for value in (self.historical_manifest_sha256, self.task_source_sha256):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise R25ArtifactBuildError(
                    "INVALID_HISTORICAL_MANIFEST", "source freeze digest is invalid"
                )
        if (
            type(self.historical_manifest_byte_count) is not int
            or self.historical_manifest_byte_count < 1
            or type(self.task_source_bytes) is not bytes
            or not self.task_source_bytes
            or hashlib.sha256(self.task_source_bytes).hexdigest() != self.task_source_sha256
            or type(self.task_count) is not int
            or self.task_count != GUI_ONLY_TASK_SOURCE_TASK_COUNT
            or type(self.trial) is not int
            or self.trial != GUI_ONLY_TASK_SOURCE_TRIAL
            or type(self.trial_provenance) is not str
            or self.trial_provenance != GUI_ONLY_TASK_SOURCE_TRIAL_PROVENANCE
        ):
            raise R25ArtifactBuildError(
                "INVALID_GUI_ONLY_TASK_SOURCE", "source freeze values are inconsistent"
            )
        rows = _source_rows(self.task_source_bytes)
        if len(rows) != self.task_count or any(row.trial != self.trial for row in rows):
            raise R25ArtifactBuildError(
                "INVALID_GUI_ONLY_TASK_SOURCE",
                "source freeze rows are inconsistent with its census and trial declaration",
            )
        canonical_source_bytes = b"".join(
            canonical_json_bytes(
                cast(
                    JsonValue,
                    {"task_name": row.task_name, "trial": row.trial},
                )
            )
            + b"\n"
            for row in rows
        )
        if self.task_source_bytes != canonical_source_bytes:
            raise R25ArtifactBuildError(
                "INVALID_GUI_ONLY_TASK_SOURCE",
                "source freeze rows are not exact canonical JSONL",
            )


def frozen_gui_only_task_source_projection_v1(
    source: FrozenGuiOnlyTaskSourceV1,
) -> dict[str, JsonValue]:
    """Project the complete provenance without duplicating the JSONL payload."""

    if type(source) is not FrozenGuiOnlyTaskSourceV1:
        raise R25ArtifactBuildError("UNTRUSTED_SOURCE_FREEZE", "source freeze type is untrusted")
    return {
        "historical_manifest_byte_count": source.historical_manifest_byte_count,
        "historical_manifest_path": source.historical_manifest_path,
        "historical_manifest_sha256": source.historical_manifest_sha256,
        "schema_version": GUI_ONLY_TASK_SOURCE_FREEZE_SCHEMA_VERSION,
        "task_count": source.task_count,
        "task_source_byte_count": len(source.task_source_bytes),
        "task_source_sha256": source.task_source_sha256,
        "trial": source.trial,
        "trial_provenance": source.trial_provenance,
    }


def frozen_gui_only_task_source_sha256_v1(source: FrozenGuiOnlyTaskSourceV1) -> str:
    return canonical_sha256(cast(JsonValue, frozen_gui_only_task_source_projection_v1(source)))


def frozen_gui_only_task_source_receipt_v1(
    source: FrozenGuiOnlyTaskSourceV1,
) -> dict[str, JsonValue]:
    projection = cast(JsonValue, frozen_gui_only_task_source_projection_v1(source))
    return {
        "source_freeze": projection,
        "source_freeze_sha256": canonical_sha256(projection),
    }


def _parse_frozen_gui_only_task_source_projection_v1(
    value: object,
    *,
    task_source_bytes: bytes,
) -> FrozenGuiOnlyTaskSourceV1:
    fields = frozenset(
        {
            "historical_manifest_byte_count",
            "historical_manifest_path",
            "historical_manifest_sha256",
            "schema_version",
            "task_count",
            "task_source_byte_count",
            "task_source_sha256",
            "trial",
            "trial_provenance",
        }
    )
    item = _exact_object(value, fields, "GUI-only task source freeze")
    if (
        item["schema_version"] != GUI_ONLY_TASK_SOURCE_FREEZE_SCHEMA_VERSION
        or type(item["task_source_byte_count"]) is not int
        or item["task_source_byte_count"] != len(task_source_bytes)
    ):
        raise R25ArtifactBuildError(
            "INVALID_GUI_ONLY_TASK_SOURCE", "source freeze projection differs"
        )
    try:
        return FrozenGuiOnlyTaskSourceV1(
            historical_manifest_path=cast(str, item["historical_manifest_path"]),
            historical_manifest_sha256=cast(str, item["historical_manifest_sha256"]),
            historical_manifest_byte_count=cast(int, item["historical_manifest_byte_count"]),
            task_source_bytes=task_source_bytes,
            task_source_sha256=cast(str, item["task_source_sha256"]),
            task_count=cast(int, item["task_count"]),
            trial=cast(int, item["trial"]),
            trial_provenance=cast(str, item["trial_provenance"]),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, R25ArtifactBuildError):
            raise
        raise R25ArtifactBuildError(
            "INVALID_GUI_ONLY_TASK_SOURCE", "source freeze projection is invalid"
        ) from exc


def _validate_source_freeze_against_historical_manifest(
    source: FrozenGuiOnlyTaskSourceV1,
) -> None:
    regenerated = freeze_gui_only_task_source_from_historical_manifest_v1(
        Path(source.historical_manifest_path),
        expected_manifest_sha256=source.historical_manifest_sha256,
        expected_manifest_byte_count=source.historical_manifest_byte_count,
    )
    if regenerated != source:
        raise R25ArtifactBuildError(
            "SOURCE_FREEZE_PROVENANCE_MISMATCH",
            "source freeze does not reproduce from its exact historical manifest",
        )


@dataclass(frozen=True, slots=True)
class ArtifactBundleReadbackValidationV1:
    bundle_directory: str
    artifact_bundle_sha256: str
    gui_only_task_source_sha256: str
    registry_sha256: str
    source_task_count: int
    cohort_size: int
    artifact_count: int

    def __post_init__(self) -> None:
        if (
            type(self.bundle_directory) is not str
            or not Path(self.bundle_directory).is_absolute()
            or type(self.source_task_count) is not int
            or self.source_task_count < 20
            or type(self.cohort_size) is not int
            or not 20 <= self.cohort_size <= 30
            or self.artifact_count != len(_WRITTEN_ARTIFACT_FILENAMES)
        ):
            raise R25ArtifactBuildError(
                "INVALID_READBACK_VALIDATION", "readback validation values are inconsistent"
            )
        for value in (
            self.artifact_bundle_sha256,
            self.gui_only_task_source_sha256,
            self.registry_sha256,
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise R25ArtifactBuildError(
                    "INVALID_READBACK_VALIDATION", "readback digest is invalid"
                )


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
    normalized = Path(os.path.abspath(os.fspath(path)))
    repository = Path(os.path.abspath(os.fspath(repository_root)))
    if _path_within(normalized, repository):
        raise R25ArtifactBuildError("REPOSITORY_PATH_FORBIDDEN", f"{name} must be repo-external")
    return normalized


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


_DirectoryChainV1 = tuple[tuple[int, str | None, int, int], ...]


def _open_directory_chain(path: Path, *, name: str) -> tuple[Path, _DirectoryChainV1]:
    """Open every lexical directory component and retain its inode identity."""

    absolute = _absolute_path(path, name)
    normalized = Path(os.path.abspath(os.fspath(absolute)))
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
        opened.append((descriptor, None, metadata.st_dev, metadata.st_ino))
        for component in normalized.parts[1:]:
            descriptor = os.open(component, flags, dir_fd=opened[-1][0])
            metadata = os.fstat(descriptor)
            named = os.stat(component, dir_fd=opened[-1][0], follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
            ):
                os.close(descriptor)
                raise R25ArtifactBuildError(
                    "PATH_IDENTITY_DRIFT", f"{name} crosses a directory alias"
                )
            opened.append((descriptor, component, metadata.st_dev, metadata.st_ino))
        return normalized, tuple(opened)
    except R25ArtifactBuildError:
        for descriptor, _, _, _ in reversed(opened):
            os.close(descriptor)
        raise
    except OSError as exc:
        for descriptor, _, _, _ in reversed(opened):
            os.close(descriptor)
        raise R25ArtifactBuildError(
            "INVALID_PATH", f"{name} cannot be opened without aliases"
        ) from exc


def _close_directory_chain(chain: _DirectoryChainV1) -> None:
    for descriptor, _, _, _ in reversed(chain):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _revalidate_directory_chain(chain: _DirectoryChainV1, *, name: str) -> None:
    for index, (descriptor, component, device, inode) in enumerate(chain):
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
            device,
            inode,
        ):
            raise R25ArtifactBuildError("PATH_IDENTITY_DRIFT", f"{name} directory identity changed")
        if index:
            assert component is not None
            named = os.stat(component, dir_fd=chain[index - 1][0], follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (device, inode):
                raise R25ArtifactBuildError("PATH_IDENTITY_DRIFT", f"{name} directory path changed")


@dataclass(frozen=True, slots=True)
class _TaskTreeDirectoryV1:
    descriptor: int
    parent_descriptor: int | None
    entry_name: str | None
    relative_path: str
    device: int
    inode: int
    entries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TaskTreeEntryV1:
    parent_descriptor: int
    entry_name: str
    relative_path: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int


@dataclass(slots=True)
class _HeldTaskDefinitionTreeV1:
    root: Path
    root_chain: _DirectoryChainV1
    directories: tuple[_TaskTreeDirectoryV1, ...]
    entries: tuple[_TaskTreeEntryV1, ...]
    sources: dict[str, bytes]

    def revalidate(self) -> None:
        _revalidate_directory_chain(self.root_chain, name="task definitions root")
        for directory in self.directories:
            current = os.fstat(directory.descriptor)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (directory.device, directory.inode)
                or tuple(
                    sorted(os.listdir(directory.descriptor), key=lambda item: item.encode("utf-8"))
                )
                != directory.entries
            ):
                raise R25ArtifactBuildError(
                    "TASK_DEFINITION_TREE_DRIFT",
                    "task definition directory changed during registry snapshot",
                )
            if directory.parent_descriptor is not None:
                assert directory.entry_name is not None
                named = os.stat(
                    directory.entry_name,
                    dir_fd=directory.parent_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
                    directory.device,
                    directory.inode,
                ):
                    raise R25ArtifactBuildError(
                        "TASK_DEFINITION_TREE_DRIFT",
                        "task definition directory binding changed",
                    )
        for entry in self.entries:
            named = os.stat(
                entry.entry_name,
                dir_fd=entry.parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or named.st_uid != os.geteuid()
                or named.st_gid != os.getegid()
                or (named.st_dev, named.st_ino) != (entry.device, entry.inode)
                or named.st_mode != entry.mode
                or named.st_size != entry.size
                or named.st_mtime_ns != entry.mtime_ns
            ):
                raise R25ArtifactBuildError(
                    "TASK_DEFINITION_TREE_DRIFT",
                    "task definition entry changed during registry snapshot",
                )

    def close(self) -> None:
        root_descriptor = self.root_chain[-1][0]
        for directory in reversed(self.directories):
            if directory.descriptor != root_descriptor:
                try:
                    os.close(directory.descriptor)
                except OSError:
                    pass
        _close_directory_chain(self.root_chain)


def _read_task_source_at(
    *, parent_descriptor: int, entry_name: str, expected: os.stat_result
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            entry_name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
            or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
            or not 0 <= before.st_size <= _MAX_TASK_DEFINITION_BYTES
        ):
            raise R25ArtifactBuildError(
                "INVALID_TASK_DEFINITION_SOURCE",
                "task definition must be an owner-held unaliased regular file",
            )
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
        named_after = os.stat(entry_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or (named_after.st_dev, named_after.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise R25ArtifactBuildError(
                "TASK_DEFINITION_TREE_DRIFT", "task definition changed while being read"
            )
        return raw
    except R25ArtifactBuildError:
        raise
    except OSError as exc:
        raise R25ArtifactBuildError(
            "INVALID_TASK_DEFINITION_SOURCE", "task definition cannot be read safely"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _snapshot_task_definition_tree_v1(root: Path) -> _HeldTaskDefinitionTreeV1:
    """Hold one alias-free directory census and read every Python source via openat."""

    normalized, root_chain = _open_directory_chain(root, name="task definitions root")
    root_descriptor = root_chain[-1][0]
    directories: list[_TaskTreeDirectoryV1] = []
    entries: list[_TaskTreeEntryV1] = []
    sources: dict[str, bytes] = {}
    opened_child_descriptors: list[int] = []
    pending: list[tuple[int, int | None, str | None, Path]] = [
        (root_descriptor, None, None, Path("."))
    ]
    try:
        while pending:
            descriptor, parent_descriptor, entry_name, relative = pending.pop()
            info = os.fstat(descriptor)
            names = tuple(sorted(os.listdir(descriptor), key=lambda item: item.encode("utf-8")))
            directories.append(
                _TaskTreeDirectoryV1(
                    descriptor=descriptor,
                    parent_descriptor=parent_descriptor,
                    entry_name=entry_name,
                    relative_path=relative.as_posix(),
                    device=info.st_dev,
                    inode=info.st_ino,
                    entries=names,
                )
            )
            if len(directories) + len(entries) + len(names) > _MAX_SOURCE_ROWS:
                raise R25ArtifactBuildError(
                    "TASK_DEFINITION_TREE_LIMIT",
                    "task definition tree exceeds its fixed census bound",
                )
            child_directories: list[tuple[int, int, str, Path]] = []
            for name in names:
                named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child_relative = Path(name) if relative == Path(".") else relative / name
                if stat.S_ISLNK(named.st_mode):
                    raise R25ArtifactBuildError(
                        "TASK_DEFINITION_ALIAS_FORBIDDEN",
                        "task definition tree contains a symbolic link",
                    )
                if stat.S_ISDIR(named.st_mode):
                    child = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                    child_info = os.fstat(child)
                    if (child_info.st_dev, child_info.st_ino) != (
                        named.st_dev,
                        named.st_ino,
                    ):
                        os.close(child)
                        raise R25ArtifactBuildError(
                            "TASK_DEFINITION_TREE_DRIFT",
                            "task definition directory changed while opening",
                        )
                    opened_child_descriptors.append(child)
                    child_directories.append((child, descriptor, name, child_relative))
                    continue
                if not stat.S_ISREG(named.st_mode):
                    raise R25ArtifactBuildError(
                        "TASK_DEFINITION_ALIAS_FORBIDDEN",
                        "task definition tree contains a non-regular entry",
                    )
                if (
                    named.st_nlink != 1
                    or named.st_uid != os.geteuid()
                    or named.st_gid != os.getegid()
                ):
                    raise R25ArtifactBuildError(
                        "TASK_DEFINITION_ALIAS_FORBIDDEN",
                        "task definition tree contains an aliased or foreign file",
                    )
                entries.append(
                    _TaskTreeEntryV1(
                        parent_descriptor=descriptor,
                        entry_name=name,
                        relative_path=child_relative.as_posix(),
                        device=named.st_dev,
                        inode=named.st_ino,
                        mode=named.st_mode,
                        size=named.st_size,
                        mtime_ns=named.st_mtime_ns,
                    )
                )
                if name.endswith(".py"):
                    sources[child_relative.as_posix()] = _read_task_source_at(
                        parent_descriptor=descriptor,
                        entry_name=name,
                        expected=named,
                    )
            pending.extend(reversed(child_directories))
        held = _HeldTaskDefinitionTreeV1(
            root=normalized,
            root_chain=root_chain,
            directories=tuple(directories),
            entries=tuple(entries),
            sources=sources,
        )
        held.revalidate()
        return held
    except Exception:
        for descriptor in reversed(opened_child_descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        _close_directory_chain(root_chain)
        raise


def _read_regular_file(path: Path, *, maximum: int, name: str) -> bytes:
    path = _absolute_path(path, name)
    descriptor = -1
    chain: _DirectoryChainV1 = ()
    try:
        normalized, chain = _open_directory_chain(path.parent, name=f"{name} parent")
        del normalized
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, dir_fd=chain[-1][0])
        before = os.fstat(descriptor)
        named_before = os.stat(path.name, dir_fd=chain[-1][0], follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(named_before.st_mode)
            or (before.st_dev, before.st_ino) != (named_before.st_dev, named_before.st_ino)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
        ):
            raise R25ArtifactBuildError(
                "INVALID_FILE", f"{name} must be an owner-held, unaliased regular file"
            )
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
        _revalidate_directory_chain(chain, name=f"{name} parent")
        named_after = os.stat(path.name, dir_fd=chain[-1][0], follow_symlinks=False)
    except R25ArtifactBuildError:
        raise
    except OSError as exc:
        raise R25ArtifactBuildError("UNREADABLE_FILE", f"{name} cannot be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_directory_chain(chain)
    if (
        len(raw) != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ino != before.st_ino
        or after.st_dev != before.st_dev
        or (named_after.st_dev, named_after.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise R25ArtifactBuildError("FILE_DRIFT", f"{name} changed while being read")
    return raw


def _fsync_directory(path: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        os.fsync(descriptor)
    except OSError as exc:
        raise R25ArtifactBuildError(
            "ARTIFACT_FSYNC_FAILED", "artifact directory durability barrier failed"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_fresh_owner_file(path: Path, payload: bytes, *, name: str) -> None:
    descriptor = -1
    chain: _DirectoryChainV1 = ()
    try:
        _, chain = _open_directory_chain(path.parent, name=f"{name} parent")
        try:
            os.stat(path.name, dir_fd=chain[-1][0], follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise R25ArtifactBuildError("OUTPUT_NOT_FRESH", f"{name} must not exist")
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=chain[-1][0],
        )
        opened = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=chain[-1][0], follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
            or opened.st_gid != os.getegid()
        ):
            raise R25ArtifactBuildError(
                "ARTIFACT_WRITE_FAILED", f"{name} identity differs after creation"
            )
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("short artifact write")
            written += count
        os.fsync(descriptor)
        os.fsync(chain[-1][0])
        _revalidate_directory_chain(chain, name=f"{name} parent")
        rebound = os.stat(path.name, dir_fd=chain[-1][0], follow_symlinks=False)
        after = os.fstat(descriptor)
        if (
            (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != len(payload)
        ):
            raise R25ArtifactBuildError("PATH_IDENTITY_DRIFT", f"{name} changed during publication")
    except R25ArtifactBuildError:
        raise
    except OSError as exc:
        raise R25ArtifactBuildError("ARTIFACT_WRITE_FAILED", f"{name} publication failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_directory_chain(chain)


def _read_owner_only_artifact(path: Path, *, name: str) -> bytes:
    descriptor = -1
    chain: _DirectoryChainV1 = ()
    try:
        _, chain = _open_directory_chain(path.parent, name=f"{name} parent")
        lexical = os.stat(path.name, dir_fd=chain[-1][0], follow_symlinks=False)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=chain[-1][0],
        )
        before = os.fstat(descriptor)
        identity_fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size")
        if any(getattr(lexical, field) != getattr(before, field) for field in identity_fields):
            raise R25ArtifactBuildError(
                "ARTIFACT_READBACK_FAILED", f"{name} changed while being opened"
            )
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_ARTIFACT_BYTES
        ):
            raise R25ArtifactBuildError(
                "ARTIFACT_READBACK_FAILED", f"{name} is not an owner-only single-link file"
            )
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
        _revalidate_directory_chain(chain, name=f"{name} parent")
        rebound = os.stat(path.name, dir_fd=chain[-1][0], follow_symlinks=False)
    except OSError as exc:
        raise R25ArtifactBuildError("ARTIFACT_READBACK_FAILED", f"{name} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_directory_chain(chain)
    if (
        len(raw) != before.st_size
        or any(getattr(before, field) != getattr(after, field) for field in identity_fields)
        or after.st_mtime_ns != before.st_mtime_ns
        or (rebound.st_dev, rebound.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise R25ArtifactBuildError(
            "ARTIFACT_READBACK_FAILED", f"{name} changed during owner-only readback"
        )
    return raw


def _read_owner_only_artifact_at(
    directory_descriptor: int,
    filename: str,
    *,
    name: str,
) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        before = os.fstat(descriptor)
        named = os.stat(filename, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_ARTIFACT_BYTES
        ):
            raise R25ArtifactBuildError(
                "ARTIFACT_READBACK_FAILED", f"{name} is not one owner-only regular file"
            )
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        rebound = os.stat(filename, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or (rebound.st_dev, rebound.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise R25ArtifactBuildError(
                "ARTIFACT_READBACK_FAILED", f"{name} changed during readback"
            )
        return raw
    except R25ArtifactBuildError:
        raise
    except OSError as exc:
        raise R25ArtifactBuildError("ARTIFACT_READBACK_FAILED", f"{name} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_bundle_artifacts_stable(
    target: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, bytes]:
    chain: _DirectoryChainV1 = ()
    try:
        _, chain = _open_directory_chain(target, name="bundle directory")
        descriptor = chain[-1][0]
        metadata = os.fstat(descriptor)
        if expected_identity is not None and (metadata.st_dev, metadata.st_ino) != (
            expected_identity
        ):
            raise R25ArtifactBuildError(
                "PATH_IDENTITY_DRIFT", "bundle directory is not the published inode"
            )
        entries = os.listdir(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or set(entries) != _WRITTEN_ARTIFACT_FILENAMES
            or len(entries) != len(_WRITTEN_ARTIFACT_FILENAMES)
        ):
            raise R25ArtifactBuildError(
                "ARTIFACT_READBACK_FAILED",
                "bundle must be one owner-only directory containing exactly seven artifacts",
            )
        result: dict[str, bytes] = {}
        for filename in sorted(_WRITTEN_ARTIFACT_FILENAMES):
            result[filename] = _read_owner_only_artifact_at(descriptor, filename, name=filename)
            _revalidate_directory_chain(chain, name="bundle directory")
        if set(os.listdir(descriptor)) != _WRITTEN_ARTIFACT_FILENAMES:
            raise R25ArtifactBuildError(
                "ARTIFACT_READBACK_FAILED", "bundle membership changed during readback"
            )
        _revalidate_directory_chain(chain, name="bundle directory")
        return result
    finally:
        _close_directory_chain(chain)


def _exact_object(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise R25ArtifactBuildError("INVALID_JSON_OBJECT", f"{name} must be an object")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != fields:
        raise R25ArtifactBuildError("INVALID_JSON_OBJECT", f"{name} fields are not exact")
    return cast(dict[str, object], mapping)


def freeze_gui_only_task_source_from_historical_manifest_v1(
    historical_manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_manifest_byte_count: int,
) -> FrozenGuiOnlyTaskSourceV1:
    """Extract only ordered task identities from one exact historical GUI-117 manifest.

    The historical artifact supplies task names and their canonical order only.
    ``trial=1`` is created here as a new R2.5 initialization declaration; it is
    deliberately not represented as a value observed or derived from the
    historical task runs.
    """

    if (
        type(expected_manifest_sha256) is not str
        or _SHA256.fullmatch(expected_manifest_sha256) is None
        or type(expected_manifest_byte_count) is not int
        or not 1 <= expected_manifest_byte_count <= _MAX_ARTIFACT_BYTES
    ):
        raise R25ArtifactBuildError(
            "INVALID_HISTORICAL_MANIFEST_BINDING",
            "historical manifest binding is invalid",
        )
    raw = _read_regular_file(
        historical_manifest_path,
        maximum=_MAX_ARTIFACT_BYTES,
        name="historical GUI-117 manifest",
    )
    if (
        len(raw) != expected_manifest_byte_count
        or hashlib.sha256(raw).hexdigest() != expected_manifest_sha256
    ):
        raise R25ArtifactBuildError(
            "HISTORICAL_MANIFEST_DRIFT", "historical GUI-117 manifest binding changed"
        )
    value = _strict_json(raw, "historical GUI-117 manifest")
    if raw != canonical_json_bytes(cast(JsonValue, value)) + b"\n":
        raise R25ArtifactBuildError(
            "NONCANONICAL_HISTORICAL_MANIFEST",
            "historical GUI-117 manifest is not exact canonical JSON with one newline",
        )
    manifest = _exact_object(value, _CURATED_TASK_SET_FIELDS, "historical GUI-117 manifest")
    if (
        manifest["schema_version"] != _CURATED_TASK_SET_SCHEMA_VERSION
        or manifest["artifact_type"] != _CURATED_TASK_SET_ARTIFACT_TYPE
        or manifest["is_raw_run"] is not False
        or manifest["raw_schema_version"] != "mobileworld.audit.event/v1"
    ):
        raise R25ArtifactBuildError(
            "UNSUPPORTED_HISTORICAL_MANIFEST",
            "historical manifest type/schema is not the frozen curated task set",
        )
    catalog = _exact_object(
        manifest["canonical_catalog"], _CURATED_CATALOG_FIELDS, "canonical catalog"
    )
    counts = _exact_object(manifest["counts"], _CURATED_COUNT_FIELDS, "historical counts")
    policy = _exact_object(
        manifest["selection_policy"],
        _CURATED_SELECTION_POLICY_FIELDS,
        "historical selection policy",
    )
    if (
        type(catalog["task_count"]) is not int
        or catalog["task_count"] != GUI_ONLY_TASK_SOURCE_TASK_COUNT
        or type(counts["task_count"]) is not int
        or counts["task_count"] != GUI_ONLY_TASK_SOURCE_TASK_COUNT
        or policy["unit"] != "task_run"
        or policy["task_capture_complete"] is not True
        or policy["task_goal_sha256_must_match_catalog"] is not True
        or policy["missing_artifacts_must_be_empty"] is not True
        or policy["collector_error_event_ids_must_be_empty"] is not True
        or policy["raw_events_or_blobs_copied"] is not False
    ):
        raise R25ArtifactBuildError(
            "INVALID_HISTORICAL_MANIFEST",
            "historical manifest does not bind one complete 117-task identity catalog",
        )
    tasks_value = manifest["tasks"]
    if type(tasks_value) is not list or len(tasks_value) != GUI_ONLY_TASK_SOURCE_TASK_COUNT:
        raise R25ArtifactBuildError(
            "INVALID_HISTORICAL_MANIFEST", "historical task census is not exactly 117"
        )
    task_names: list[str] = []
    task_name_index: list[dict[str, JsonValue]] = []
    for expected_index, raw_task in enumerate(cast(list[object], tasks_value), start=1):
        task = _exact_object(raw_task, _CURATED_TASK_FIELDS, "historical task")
        task_index = task["canonical_suite_index"]
        task_name = task["task_name"]
        if (
            type(task_index) is not int
            or task_index != expected_index
            or type(task_name) is not str
        ):
            raise R25ArtifactBuildError(
                "INVALID_HISTORICAL_MANIFEST", "historical task identities are not contiguous"
            )
        try:
            MobileWorldTaskParametersV1(
                task_name=task_name,
                trial=GUI_ONLY_TASK_SOURCE_TRIAL,
            )
        except (TypeError, ValueError) as exc:
            raise R25ArtifactBuildError(
                "INVALID_HISTORICAL_MANIFEST", "historical task name is invalid"
            ) from exc
        if (
            task["capture_complete"] is not True
            or task["collector_error_event_ids"] != []
            or task["missing_artifacts"] != []
        ):
            raise R25ArtifactBuildError(
                "INVALID_HISTORICAL_MANIFEST", "historical selected task is incomplete"
            )
        task_names.append(task_name)
        task_name_index.append({"task_index": task_index, "task_name": task_name})
    if len(set(task_names)) != GUI_ONLY_TASK_SOURCE_TASK_COUNT:
        raise R25ArtifactBuildError(
            "INVALID_HISTORICAL_MANIFEST", "historical task identities are not unique"
        )
    task_name_index_sha256 = canonical_sha256(cast(JsonValue, task_name_index))
    selection_sha256 = canonical_sha256(cast(JsonValue, tasks_value))
    if (
        catalog["task_name_index_sha256"] != task_name_index_sha256
        or manifest["selection_sha256"] != selection_sha256
    ):
        raise R25ArtifactBuildError(
            "INVALID_HISTORICAL_MANIFEST", "historical task identity hashes are inconsistent"
        )
    source_bytes = b"".join(
        canonical_json_bytes(
            cast(
                JsonValue,
                {"task_name": task_name, "trial": GUI_ONLY_TASK_SOURCE_TRIAL},
            )
        )
        + b"\n"
        for task_name in task_names
    )
    if len(_source_rows(source_bytes)) != GUI_ONLY_TASK_SOURCE_TASK_COUNT:
        raise R25ArtifactBuildError(
            "INVALID_GUI_ONLY_TASK_SOURCE", "generated source did not round-trip"
        )
    return FrozenGuiOnlyTaskSourceV1(
        historical_manifest_path=str(historical_manifest_path),
        historical_manifest_sha256=expected_manifest_sha256,
        historical_manifest_byte_count=expected_manifest_byte_count,
        task_source_bytes=source_bytes,
        task_source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        task_count=GUI_ONLY_TASK_SOURCE_TASK_COUNT,
        trial=GUI_ONLY_TASK_SOURCE_TRIAL,
        trial_provenance=GUI_ONLY_TASK_SOURCE_TRIAL_PROVENANCE,
    )


def write_fresh_gui_only_task_source_v1(
    source: FrozenGuiOnlyTaskSourceV1,
    output_path: Path,
    *,
    repository_root: Path,
) -> Path:
    """Publish a source once; failed readback leaves the bytes untouched."""

    if type(source) is not FrozenGuiOnlyTaskSourceV1:
        raise R25ArtifactBuildError("UNTRUSTED_SOURCE_FREEZE", "source freeze type is untrusted")
    repository = _absolute_path(repository_root, "repository root").resolve(strict=True)
    output = _repo_external(output_path, repository, "GUI-only task source output")
    _write_fresh_owner_file(output, source.task_source_bytes, name="GUI-only task source")
    readback = _read_owner_only_artifact(output, name="GUI-only task source")
    if readback != source.task_source_bytes:
        raise R25ArtifactBuildError(
            "ARTIFACT_READBACK_FAILED", "task source readback differs from published bytes"
        )
    return output


def write_fresh_gui_only_task_source_freeze_receipt_v1(
    source: FrozenGuiOnlyTaskSourceV1,
    output_path: Path,
    *,
    repository_root: Path,
) -> Path:
    """Publish the canonical provenance receipt once beside the frozen JSONL."""

    if type(source) is not FrozenGuiOnlyTaskSourceV1:
        raise R25ArtifactBuildError("UNTRUSTED_SOURCE_FREEZE", "source freeze type is untrusted")
    repository = _absolute_path(repository_root, "repository root").resolve(strict=True)
    output = _repo_external(output_path, repository, "GUI-only task source freeze receipt")
    payload = canonical_json_bytes(cast(JsonValue, frozen_gui_only_task_source_receipt_v1(source)))
    _write_fresh_owner_file(output, payload, name="GUI-only task source freeze receipt")
    if _read_owner_only_artifact(output, name="GUI-only task source freeze receipt") != payload:
        raise R25ArtifactBuildError(
            "ARTIFACT_READBACK_FAILED", "source freeze receipt readback differs"
        )
    return output


def load_gui_only_task_source_freeze_receipt_v1(
    receipt_path: Path,
    *,
    task_source_path: Path,
) -> FrozenGuiOnlyTaskSourceV1:
    """Reopen a canonical receipt, its source, and the bound historical manifest."""

    receipt_raw = _read_owner_only_artifact(
        receipt_path, name="GUI-only task source freeze receipt"
    )
    source_raw = _read_owner_only_artifact(task_source_path, name="GUI-only task source")
    receipt = _strict_json(receipt_raw, "GUI-only task source freeze receipt")
    receipt_fields = frozenset({"source_freeze", "source_freeze_sha256"})
    receipt_item = _exact_object(receipt, receipt_fields, "GUI-only task source freeze receipt")
    projection = receipt_item["source_freeze"]
    if (
        receipt_raw != canonical_json_bytes(cast(JsonValue, receipt))
        or type(receipt_item["source_freeze_sha256"]) is not str
        or receipt_item["source_freeze_sha256"] != canonical_sha256(cast(JsonValue, projection))
    ):
        raise R25ArtifactBuildError(
            "INVALID_SOURCE_FREEZE_RECEIPT", "source freeze receipt is not canonical or bound"
        )
    source = _parse_frozen_gui_only_task_source_projection_v1(
        projection, task_source_bytes=source_raw
    )
    _validate_source_freeze_against_historical_manifest(source)
    return source


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


def _git_head_task_definition_blobs_v1(
    repository_root: Path, definitions_root: Path
) -> dict[str, str]:
    """Return exact HEAD blob IDs for the definitions snapshot without worktree reads."""

    try:
        prefix = definitions_root.relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise R25ArtifactBuildError(
            "INVALID_TASK_DEFINITION_ROOT",
            "task definitions root is outside the repository",
        ) from exc
    environment = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    try:
        result = subprocess.run(
            [
                "/usr/bin/git",
                "ls-tree",
                "-rz",
                "--full-tree",
                "HEAD",
                "--",
                prefix,
            ],
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise R25ArtifactBuildError(
            "GIT_STATE_UNAVAILABLE", "task definition Git tree is unavailable"
        ) from exc
    if result.returncode != 0 or result.stderr:
        raise R25ArtifactBuildError(
            "GIT_STATE_UNAVAILABLE", "task definition Git tree is unavailable"
        )
    expected: dict[str, str] = {}
    prefix_with_slash = prefix + "/"
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, object_type, raw_sha1 = metadata.split(b" ", 2)
            path = raw_path.decode("utf-8")
            mode = raw_mode.decode("ascii")
            sha1 = raw_sha1.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise R25ArtifactBuildError(
                "GIT_STATE_UNAVAILABLE", "task definition Git tree is malformed"
            ) from exc
        if (
            object_type != b"blob"
            or mode not in {"100644", "100755"}
            or _GIT_SHA1.fullmatch(sha1) is None
            or not path.startswith(prefix_with_slash)
        ):
            raise R25ArtifactBuildError(
                "TASK_DEFINITION_GIT_MISMATCH",
                "task definition Git tree contains an unsupported entry",
            )
        relative = path[len(prefix_with_slash) :]
        if relative.endswith(".py"):
            expected[relative] = sha1
    if not expected:
        raise R25ArtifactBuildError(
            "TASK_DEFINITION_GIT_MISMATCH", "HEAD contains no task definitions"
        )
    return expected


def _validate_task_definition_sources_against_git_v1(
    sources: dict[str, bytes], *, repository_root: Path, definitions_root: Path
) -> None:
    expected = _git_head_task_definition_blobs_v1(repository_root, definitions_root)
    if set(sources) != set(expected):
        raise R25ArtifactBuildError(
            "TASK_DEFINITION_GIT_MISMATCH",
            "task definition source census differs from HEAD",
        )
    for relative, raw in sources.items():
        blob_preimage = b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
        actual = hashlib.sha1(blob_preimage, usedforsecurity=False).hexdigest()
        if actual != expected[relative]:
            raise R25ArtifactBuildError(
                "TASK_DEFINITION_GIT_MISMATCH",
                "task definition source bytes differ from HEAD",
            )


def _load_registry_tasks_from_snapshots_v1(
    sources: dict[str, bytes], *, definitions_root: Path
) -> tuple[dict[str, object], dict[str, bytes]]:
    """Execute only Git-matched in-memory bytes; no definition path is imported."""

    from mobile_world.tasks.base import BaseTask

    tasks: dict[str, object] = {}
    source_by_task: dict[str, bytes] = {}
    for relative in sorted(sources, key=lambda item: item.encode("utf-8")):
        if Path(relative).name == "__init__.py":
            continue
        raw = sources[relative]
        module_name = "mobile_world.tasks.definitions." + Path(relative).with_suffix(
            ""
        ).as_posix().replace("/", ".")
        module = ModuleType(module_name)
        module.__file__ = str(definitions_root / relative)
        module.__package__ = module_name.rpartition(".")[0]
        try:
            code = compile(raw, module.__file__, "exec", dont_inherit=True)
            exec(code, module.__dict__)
        except Exception as exc:
            raise R25ArtifactBuildError(
                "TASK_DEFINITION_SOURCE_INVALID",
                "Git-matched task definition source cannot be loaded",
            ) from exc
        for name, candidate in inspect.getmembers(module, inspect.isclass):
            if (
                not issubclass(candidate, BaseTask)
                or candidate is BaseTask
                or candidate.__module__ != module_name
            ):
                continue
            if name in tasks:
                raise R25ArtifactBuildError(
                    "DUPLICATE_TASK_DEFINITION_SOURCE",
                    "task class name appears in more than one source file",
                )
            try:
                tasks[name] = candidate()
            except Exception as exc:
                raise R25ArtifactBuildError(
                    "TASK_DEFINITION_SOURCE_INVALID",
                    "Git-matched task definition cannot be instantiated",
                ) from exc
            source_by_task[name] = raw
    if not tasks:
        raise R25ArtifactBuildError("EMPTY_REGISTRY", "task registry is empty")
    return tasks, source_by_task


def current_registry_metadata() -> tuple[RegistryTaskMetadataV1, ...]:
    """Load only local task definitions and return their selection metadata."""

    from mobile_world.tasks.base import BaseTask

    definitions_root = Path(__file__).absolute().parents[3] / "tasks" / "definitions"
    repository_root = Path(__file__).absolute().parents[6]
    held = _snapshot_task_definition_tree_v1(definitions_root)
    try:
        _validate_task_definition_sources_against_git_v1(
            held.sources,
            repository_root=repository_root,
            definitions_root=definitions_root,
        )
        tasks, definition_sources = _load_registry_tasks_from_snapshots_v1(
            held.sources, definitions_root=definitions_root
        )
        definition_dynamic_time: dict[str, bool] = {}
        for task_id, source_raw in definition_sources.items():
            try:
                syntax = ast.parse(source_raw, filename=f"{task_id}.py")
            except (SyntaxError, ValueError) as exc:
                raise R25ArtifactBuildError(
                    "TASK_DEFINITION_SOURCE_INVALID",
                    "task definition source cannot be audited",
                ) from exc
            definition_dynamic_time[task_id] = _source_ast_uses_dynamic_wall_clock(syntax)
        held.revalidate()
        _validate_task_definition_sources_against_git_v1(
            held.sources,
            repository_root=repository_root,
            definitions_root=definitions_root,
        )
    finally:
        held.close()
    records: list[RegistryTaskMetadataV1] = []
    for task_id in sorted(tasks, key=lambda item: item.encode("utf-8")):
        task = cast(BaseTask, tasks[task_id])
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
            or definition_dynamic_time.get(task_id, True)
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

    if inputs.source_freeze_receipt is None:
        raise R25ArtifactBuildError(
            "SOURCE_FREEZE_RECEIPT_REQUIRED",
            "production authority construction requires historical GUI-117 provenance",
        )
    source_freeze = load_gui_only_task_source_freeze_receipt_v1(
        inputs.source_freeze_receipt,
        task_source_path=inputs.source_task_jsonl,
    )
    source_task_jsonl_bytes = source_freeze.task_source_bytes
    bundled_source_path = bundle_directory / GUI_ONLY_TASK_SOURCE_FILENAME
    selection = select_gui_only_cohort_from_bytes(
        source_task_jsonl_bytes,
        bundled_source_path,
        registry_records,
        cohort_size=inputs.cohort_size,
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
    # The first Joint-Sentinel decision makes exactly two rubric calls in
    # GENERATE -> TRACK order and no history call.  Every later decision makes
    # exactly one TRACK rubric call plus one HISTORY_POLICY call.
    max_openai_calls = 2 * joint_cell_count * inputs.max_steps_per_cell
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
        resource_topology=inputs.resource_topology,
        runtime_config_sha256=inputs.runtime_config_sha256,
        pricing_sha256=inputs.pricing_sha256,
        sentinel_config_sha256=production_sentinel_config_sha256_v1(),
        output_root=str(runtime_output_root),
        max_resource_preflight_wall_time_seconds=inputs.resource_preflight_wall_time_seconds,
        max_resource_cleanup_wall_time_seconds=(inputs.max_resource_cleanup_wall_time_seconds),
        resource_cleanup_upper_bound_sha256=(inputs.resource_cleanup_upper_bound_sha256),
        max_model_switches=(
            2 * len(pilot.tasks) + 1
            if inputs.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
            else 0
        ),
        max_model_switch_wall_time_seconds=(inputs.max_model_switch_wall_time_seconds),
        max_total_model_switch_wall_time_seconds=(
            (
                2 * len(pilot.tasks) + 1
                if inputs.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
                else 0
            )
            * inputs.max_model_switch_wall_time_seconds
        ),
        max_post_run_integrity_wall_time_seconds=(inputs.max_post_run_integrity_wall_time_seconds),
        max_sequence_wall_time_seconds=(
            inputs.resource_preflight_wall_time_seconds
            + smoke_wall_time
            + inputs.max_total_wall_time_seconds
            + (
                (
                    2 * len(pilot.tasks) + 1
                    if inputs.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
                    else 0
                )
                * inputs.max_model_switch_wall_time_seconds
            )
            + inputs.max_resource_cleanup_wall_time_seconds
            + inputs.max_post_run_integrity_wall_time_seconds
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
        source_freeze=source_freeze,
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
    source_freeze_projection = cast(
        JsonValue, frozen_gui_only_task_source_projection_v1(bundle.source_freeze)
    )
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
        or bundle.source_freeze.task_source_bytes != bundle.source_task_jsonl_bytes
        or bundle.source_freeze.task_source_sha256 != source_sha256
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
        "gui_only_task_source_freeze": source_freeze_projection,
        "gui_only_task_source_freeze_sha256": canonical_sha256(source_freeze_projection),
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


def _schema_values(repository_root: Path) -> tuple[dict[str, object], ...]:
    schemas: list[dict[str, object]] = []
    for relative_path in _SCHEMA_RELATIVE_PATHS:
        raw = _read_regular_file(
            repository_root / relative_path,
            maximum=_MAX_ARTIFACT_BYTES,
            name="checked-in artifact schema",
        )
        value = _strict_json(raw, "checked-in artifact schema")
        if type(value) is not dict:
            raise R25ArtifactBuildError(
                "INVALID_ARTIFACT_SCHEMA", "checked-in artifact schema is not an object"
            )
        schema = cast(dict[str, object], value)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise R25ArtifactBuildError(
                "INVALID_ARTIFACT_SCHEMA", "checked-in artifact schema failed meta-validation"
            ) from exc
        schemas.append(schema)
    return tuple(schemas)


def validate_written_artifact_bundle_v1(
    bundle_directory: Path,
    *,
    repository_root: Path,
    expected_source_commit: str | None = None,
    allow_unverified_cpu_fixture: bool = False,
    _expected_bundle_identity: tuple[int, int] | None = None,
) -> ArtifactBundleReadbackValidationV1:
    """Independently reopen and validate the complete seven-file bundle.

    This function accepts no in-memory bundle.  It recomputes current registry
    metadata and every source/selection/manifest/hash binding from the durable
    bytes.  A failure is non-repairing: callers must retain this directory and
    choose a fresh path for a later publication.
    """

    if type(allow_unverified_cpu_fixture) is not bool:
        raise R25ArtifactBuildError(
            "INVALID_VERIFICATION_MODE", "verification mode must be exact bool"
        )
    if expected_source_commit is None and not allow_unverified_cpu_fixture:
        raise R25ArtifactBuildError(
            "SOURCE_STATE_CONFIRMATION_REQUIRED",
            "durable bundle validation requires exact clean-HEAD confirmation",
        )
    repository = _absolute_path(repository_root, "repository root").resolve(strict=True)
    if expected_source_commit is not None:
        verify_current_source_commit(repository, expected_source_commit)
    target = _repo_external(bundle_directory, repository, "bundle directory")
    raw_by_name = _read_bundle_artifacts_stable(target, expected_identity=_expected_bundle_identity)
    value_by_name: dict[str, object] = {}
    for filename in _WRITTEN_ARTIFACT_FILENAMES - {GUI_ONLY_TASK_SOURCE_FILENAME}:
        raw = raw_by_name[filename]
        value = _strict_json(raw, filename)
        if raw != canonical_json_bytes(cast(JsonValue, value)):
            raise R25ArtifactBuildError(
                "NONCANONICAL_ARTIFACT", f"{filename} is not exact canonical JSON"
            )
        value_by_name[filename] = value

    schemas = _schema_values(repository)
    schema_store = {str(schema["$id"]): schema for schema in schemas}
    authority_schema_version = cast(
        dict[str, object], value_by_name[RUN_AUTHORITY_MANIFEST_FILENAME]
    ).get("schema_version")
    if type(authority_schema_version) is not str:
        raise R25ArtifactBuildError(
            "INVALID_ARTIFACT_SCHEMA", "authority manifest schema version is unknown"
        )
    authority_schema_id = {
        R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V1: (
            "https://agentsentinel.local/schemas/r2_4/run_authority_manifest.v1.schema.json"
        ),
        R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V2: (
            "https://agentsentinel.local/schemas/r2_4/run_authority_manifest.v2.schema.json"
        ),
    }.get(authority_schema_version)
    if authority_schema_id is None:
        raise R25ArtifactBuildError(
            "INVALID_ARTIFACT_SCHEMA", "authority manifest schema version is unknown"
        )
    frozen_pilot_schema_version = cast(
        dict[str, object], value_by_name[FROZEN_PILOT_MANIFEST_FILENAME]
    ).get("schema_version")
    if type(frozen_pilot_schema_version) is not str:
        raise R25ArtifactBuildError(
            "INVALID_ARTIFACT_SCHEMA", "frozen pilot schema version is unknown"
        )
    frozen_pilot_schema_id = {
        FROZEN_PILOT_SCHEMA_VERSION_V1: (
            "https://agentsentinel.local/schemas/r2_5/frozen_pilot_manifest.v1.schema.json"
        ),
        FROZEN_PILOT_SCHEMA_VERSION_V2: (
            "https://agentsentinel.local/schemas/r2_5/frozen_pilot_manifest.v2.schema.json"
        ),
    }.get(frozen_pilot_schema_version)
    if frozen_pilot_schema_id is None:
        raise R25ArtifactBuildError(
            "INVALID_ARTIFACT_SCHEMA", "frozen pilot schema version is unknown"
        )
    schema_by_filename = {
        COHORT_SELECTION_FILENAME: (
            "https://agentsentinel.local/schemas/r2_5/cohort_selection.v1.schema.json"
        ),
        PILOT_TASK_SOURCE_FILENAME: (
            "https://agentsentinel.local/schemas/r2_5/executable_task_source.v1.schema.json"
        ),
        FROZEN_PILOT_MANIFEST_FILENAME: frozen_pilot_schema_id,
        RUN_AUTHORITY_MANIFEST_FILENAME: authority_schema_id,
        TOPOLOGY_COMPARISON_FILENAME: (
            "https://agentsentinel.local/schemas/r2_4/cpu_topology_artifact.v1.schema.json"
        ),
        ARTIFACT_BUNDLE_FILENAME: (
            "https://agentsentinel.local/schemas/r2_5/artifact_bundle.v1.schema.json"
        ),
    }
    try:
        for filename, schema_id in schema_by_filename.items():
            schema = schema_store[schema_id]
            validator = Draft202012Validator(
                schema,
                resolver=RefResolver.from_schema(schema, store=schema_store),
            )
            validator.validate(value_by_name[filename])
    except (KeyError, SchemaError, ValidationError) as exc:
        raise R25ArtifactBuildError(
            "ARTIFACT_SCHEMA_VALIDATION_FAILED", "a durable artifact failed its full schema"
        ) from exc

    try:
        selection = parse_cohort_selection(value_by_name[COHORT_SELECTION_FILENAME])
        pilot = parse_frozen_pilot_manifest(value_by_name[FROZEN_PILOT_MANIFEST_FILENAME])
        authority = parse_authority_manifest(value_by_name[RUN_AUTHORITY_MANIFEST_FILENAME])
        if authority.schema_version != R24_R25_RUN_AUTHORITY_SCHEMA_VERSION_V2:
            raise R25ArtifactBuildError(
                "INVALID_ARTIFACT_SCHEMA",
                "production artifact bundles require an exact v2 authority manifest",
            )
        assert authority.resource_topology is not None
        assert authority.runtime_config_sha256 is not None
        assert authority.pricing_sha256 is not None
        assert authority.sentinel_config_sha256 is not None
        assert authority.max_resource_cleanup_wall_time_seconds is not None
        assert authority.resource_cleanup_upper_bound_sha256 is not None
        assert authority.max_model_switches is not None
        assert authority.max_model_switch_wall_time_seconds is not None
        assert authority.max_total_model_switch_wall_time_seconds is not None
        assert authority.max_post_run_integrity_wall_time_seconds is not None
        topology = parse_r24_cpu_topology_artifact(value_by_name[TOPOLOGY_COMPARISON_FILENAME])
        source_raw = raw_by_name[GUI_ONLY_TASK_SOURCE_FILENAME]
        bundle_envelope = cast(dict[str, object], value_by_name[ARTIFACT_BUNDLE_FILENAME])
        bundle_projection = cast(dict[str, object], bundle_envelope["artifact_bundle"])
        source_freeze = _parse_frozen_gui_only_task_source_projection_v1(
            bundle_projection["gui_only_task_source_freeze"],
            task_source_bytes=source_raw,
        )
        if bundle_projection["gui_only_task_source_freeze_sha256"] != (
            frozen_gui_only_task_source_sha256_v1(source_freeze)
        ):
            raise R25ArtifactBuildError(
                "SOURCE_FREEZE_PROVENANCE_MISMATCH",
                "durable bundle source-freeze hash differs",
            )
        _validate_source_freeze_against_historical_manifest(source_freeze)
        recomputed_selection = select_gui_only_cohort_from_bytes(
            source_raw,
            target / GUI_ONLY_TASK_SOURCE_FILENAME,
            current_registry_metadata(),
            cohort_size=len(selection.members),
        )
    except R25ArtifactBuildError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise R25ArtifactBuildError(
            "ARTIFACT_READBACK_FAILED", "durable artifact reconstruction failed closed"
        ) from exc
    expected_cell_order = tuple(
        (
            member.task_id,
            member.task_parameters_sha256,
            member.reset_seed,
            host,
            arm,
            "OFF" if arm is PilotArmV1.BASELINE else "ACTIVE",
        )
        for member in selection.members
        for host in (PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI)
        for arm in (PilotArmV1.BASELINE, PilotArmV1.JOINT_SENTINEL)
    )
    actual_cell_order = tuple(
        (
            cell.task_id,
            cell.task_parameters_sha256,
            cell.reset_seed,
            cell.host,
            cell.arm,
            cell.sentinel_mode,
        )
        for cell in pilot.cells
    )
    expected_task_order = tuple(
        (member.task_id, member.trial, member.task_parameters_sha256, member.reset_seed)
        for member in selection.members
    )
    cell_count = 4 * len(selection.members)
    joint_cell_count = 2 * len(selection.members)
    expected_pilot_actor_calls = cell_count * pilot.max_steps_per_cell
    expected_pilot_openai_calls = 2 * joint_cell_count * pilot.max_steps_per_cell
    smoke_actor_calls = sum(
        case.max_actor_calls for plan in authority.smoke_plans for case in plan.cases
    )
    smoke_openai_calls = sum(
        case.max_openai_calls for plan in authority.smoke_plans for case in plan.cases
    )
    smoke_cost = sum(
        case.max_cost_usd_micros for plan in authority.smoke_plans for case in plan.cases
    )
    smoke_wall_time = sum(
        case.max_wall_time_seconds for plan in authority.smoke_plans for case in plan.cases
    )
    expected_model_switches = (
        2 * len(selection.members) + 1
        if authority.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
        else 0
    )
    expected_total_model_switch_wall_time = (
        expected_model_switches * authority.max_model_switch_wall_time_seconds
    )
    expected_executable_task_source = executable_pilot_task_source_projection(
        pilot.cohort_id,
        tuple(
            PilotTaskV1(
                task_id=member.task_id,
                task_parameters_sha256=member.task_parameters_sha256,
                reset_seed=member.reset_seed,
            )
            for member in selection.members
        ),
        tuple(
            InlinePilotTaskParametersV1(
                task_id=member.task_id,
                parameters=MobileWorldTaskParametersV1(
                    task_name=member.task_id,
                    trial=member.trial,
                ),
            )
            for member in selection.members
        ),
    )
    task_source_raw = raw_by_name[PILOT_TASK_SOURCE_FILENAME]
    if (
        cohort_selection_projection(recomputed_selection) != cohort_selection_projection(selection)
        or selection.source_path != str(target / GUI_ONLY_TASK_SOURCE_FILENAME)
        or pilot.task_manifest_path != str(target / PILOT_TASK_SOURCE_FILENAME)
        or pilot.cohort_selection_artifact_path != str(target / COHORT_SELECTION_FILENAME)
        or pilot.topology_comparison_artifact_path != str(target / TOPOLOGY_COMPARISON_FILENAME)
        or authority.authorization.status is not RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED
        or authority.pilot != pilot
        or authority.topology_comparison_artifact_sha256
        != r24_cpu_topology_artifact_sha256(topology)
        or value_by_name[PILOT_TASK_SOURCE_FILENAME] != expected_executable_task_source
        or pilot.task_manifest_sha256 != hashlib.sha256(task_source_raw).hexdigest()
        or pilot.task_manifest_byte_count != len(task_source_raw)
        or len(pilot.tasks) != len(selection.members)
        or tuple(
            (task.task_id, member.trial, task.task_parameters_sha256, task.reset_seed)
            for task, member in zip(pilot.tasks, selection.members)
        )
        != expected_task_order
        or actual_cell_order != expected_cell_order
        or len(actual_cell_order) != cell_count
        or pilot.max_total_actor_calls != expected_pilot_actor_calls
        or pilot.max_total_openai_calls != expected_pilot_openai_calls
        or authority.max_sequence_actor_calls != smoke_actor_calls + pilot.max_total_actor_calls
        or authority.max_sequence_openai_calls != smoke_openai_calls + pilot.max_total_openai_calls
        or authority.max_sequence_cost_usd_micros != smoke_cost + pilot.max_total_cost_usd_micros
        or authority.max_model_switches != expected_model_switches
        or authority.max_total_model_switch_wall_time_seconds
        != expected_total_model_switch_wall_time
        or (
            authority.resource_topology == "SINGLE_GPU_SEQUENTIAL_SHARED"
            and authority.max_model_switch_wall_time_seconds <= 0
        )
        or (
            authority.resource_topology == "INDEPENDENT_GPU_CONCURRENT"
            and authority.max_model_switch_wall_time_seconds != 0
        )
        or authority.max_resource_cleanup_wall_time_seconds <= 0
        or _SHA256.fullmatch(authority.resource_cleanup_upper_bound_sha256) is None
        or _SHA256.fullmatch(authority.runtime_config_sha256) is None
        or _SHA256.fullmatch(authority.pricing_sha256) is None
        or authority.sentinel_config_sha256 != production_sentinel_config_sha256_v1()
        or authority.max_post_run_integrity_wall_time_seconds <= 0
        or authority.max_sequence_wall_time_seconds
        != (
            authority.max_resource_preflight_wall_time_seconds
            + smoke_wall_time
            + pilot.max_total_wall_time_seconds
            + expected_total_model_switch_wall_time
            + authority.max_resource_cleanup_wall_time_seconds
            + authority.max_post_run_integrity_wall_time_seconds
        )
    ):
        raise R25ArtifactBuildError(
            "ARTIFACT_BINDING_MISMATCH",
            "durable source, selection, pilot, authority, or topology bindings differ",
        )

    selection_value = cast(JsonValue, cohort_selection_projection(selection))
    task_source_value = cast(JsonValue, value_by_name[PILOT_TASK_SOURCE_FILENAME])
    pilot_value = cast(JsonValue, frozen_pilot_manifest_projection(pilot))
    authority_value = cast(JsonValue, authority_manifest_projection(authority))
    topology_value = cast(JsonValue, r24_cpu_topology_artifact_projection(topology))
    selection_raw = raw_by_name[COHORT_SELECTION_FILENAME]
    topology_raw = raw_by_name[TOPOLOGY_COMPARISON_FILENAME]
    source_sha256 = hashlib.sha256(source_raw).hexdigest()
    source_freeze_value = cast(JsonValue, frozen_gui_only_task_source_projection_v1(source_freeze))
    expected_projection: dict[str, JsonValue] = {
        "authority_manifest": authority_value,
        "authority_manifest_sha256": canonical_sha256(authority_value),
        "authority_status": RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED.value,
        "cohort_selection": selection_value,
        "cohort_selection_artifact_byte_count": len(selection_raw),
        "cohort_selection_artifact_sha256": hashlib.sha256(selection_raw).hexdigest(),
        "execution_census": {
            "actor_model_calls": 0,
            "backend_operations": 0,
            "docker_operations": 0,
            "gpu_operations": 0,
            "gui_actions": 0,
            "network_calls": 0,
            "secret_content_reads": 0,
        },
        "executable_task_source": task_source_value,
        "executable_task_source_byte_count": len(task_source_raw),
        "executable_task_source_schema_version": (EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION),
        "executable_task_source_sha256": hashlib.sha256(task_source_raw).hexdigest(),
        "frozen_pilot_manifest": pilot_value,
        "frozen_pilot_manifest_sha256": canonical_sha256(pilot_value),
        "gui_only_task_source_byte_count": len(source_raw),
        "gui_only_task_source_freeze": source_freeze_value,
        "gui_only_task_source_freeze_sha256": canonical_sha256(source_freeze_value),
        "gui_only_task_source_sha256": source_sha256,
        "schema_version": ARTIFACT_BUNDLE_SCHEMA_VERSION,
        "topology_comparison_artifact": topology_value,
        "topology_comparison_artifact_byte_count": len(topology_raw),
        "topology_comparison_artifact_sha256": hashlib.sha256(topology_raw).hexdigest(),
    }
    expected_output: dict[str, JsonValue] = {
        "artifact_bundle": expected_projection,
        "artifact_bundle_sha256": canonical_sha256(cast(JsonValue, expected_projection)),
    }
    if value_by_name[ARTIFACT_BUNDLE_FILENAME] != expected_output:
        raise R25ArtifactBuildError(
            "ARTIFACT_BUNDLE_MISMATCH", "durable bundle is not the exact recomputed projection"
        )
    if expected_source_commit is not None:
        if authority.source_commit != expected_source_commit:
            raise R25ArtifactBuildError(
                "SOURCE_COMMIT_MISMATCH", "durable authority source commit differs"
            )
        verify_current_source_commit(repository, expected_source_commit)
    return ArtifactBundleReadbackValidationV1(
        bundle_directory=str(target),
        artifact_bundle_sha256=cast(str, expected_output["artifact_bundle_sha256"]),
        gui_only_task_source_sha256=source_sha256,
        registry_sha256=selection.registry_sha256,
        source_task_count=selection.source_task_count,
        cohort_size=len(selection.members),
        artifact_count=len(_WRITTEN_ARTIFACT_FILENAMES),
    )


def write_artifact_bundle(
    bundle: AuthorityArtifactBundleV1,
    *,
    repository_root: Path,
    expected_source_commit: str | None = None,
    allow_unverified_cpu_fixture: bool = False,
) -> tuple[Path, ...]:
    """Write once into the explicitly planned fresh, repo-external directory."""

    if type(allow_unverified_cpu_fixture) is not bool:
        raise R25ArtifactBuildError(
            "INVALID_VERIFICATION_MODE", "verification mode must be exact bool"
        )
    if expected_source_commit is None and not allow_unverified_cpu_fixture:
        raise R25ArtifactBuildError(
            "SOURCE_STATE_CONFIRMATION_REQUIRED",
            "durable bundle publication requires exact clean-HEAD confirmation",
        )
    if expected_source_commit is not None:
        if bundle.authority_manifest.source_commit != expected_source_commit:
            raise R25ArtifactBuildError(
                "SOURCE_COMMIT_MISMATCH", "bundle authority source commit differs"
            )
        verify_current_source_commit(repository_root, expected_source_commit)
    target = _repo_external(
        Path(bundle.pilot_manifest.task_manifest_path).parent,
        repository_root,
        "bundle directory",
    )
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
    parent_chain: _DirectoryChainV1 = ()
    target_descriptor = -1
    published_identity: tuple[int, int] | None = None
    try:
        _, parent_chain = _open_directory_chain(target.parent, name="bundle directory parent")
        try:
            os.mkdir(target.name, mode=0o700, dir_fd=parent_chain[-1][0])
        except FileExistsError as exc:
            raise R25ArtifactBuildError(
                "OUTPUT_DIRECTORY_NOT_FRESH",
                "bundle directory must be a fresh direct child",
            ) from exc
        target_descriptor = os.open(
            target.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_chain[-1][0],
        )
        target_metadata = os.fstat(target_descriptor)
        published_identity = (target_metadata.st_dev, target_metadata.st_ino)
        target_named = os.stat(target.name, dir_fd=parent_chain[-1][0], follow_symlinks=False)
        if (
            not stat.S_ISDIR(target_metadata.st_mode)
            or (target_metadata.st_dev, target_metadata.st_ino)
            != (target_named.st_dev, target_named.st_ino)
            or stat.S_IMODE(target_metadata.st_mode) != 0o700
            or target_metadata.st_uid != os.geteuid()
            or target_metadata.st_gid != os.getegid()
        ):
            raise R25ArtifactBuildError(
                "ARTIFACT_WRITE_FAILED", "fresh bundle directory identity differs"
            )
        for filename, payload in payloads:
            path = target / filename
            descriptor = os.open(
                filename,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=target_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                view = memoryview(payload)
                offset = 0
                while offset < len(view):
                    count = os.write(descriptor, view[offset:])
                    if count <= 0:
                        raise OSError("short artifact write")
                    offset += count
                os.fsync(descriptor)
                rebound = os.stat(filename, dir_fd=target_descriptor, follow_symlinks=False)
                after = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino)
                    or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                    or after.st_nlink != 1
                    or stat.S_IMODE(after.st_mode) != 0o600
                    or after.st_size != len(payload)
                ):
                    raise R25ArtifactBuildError(
                        "PATH_IDENTITY_DRIFT", f"{filename} changed during publication"
                    )
            finally:
                os.close(descriptor)
            written.append(path)
        os.fsync(target_descriptor)
        os.fsync(parent_chain[-1][0])
        _revalidate_directory_chain(parent_chain, name="bundle directory parent")
        rebound_target = os.stat(target.name, dir_fd=parent_chain[-1][0], follow_symlinks=False)
        if (rebound_target.st_dev, rebound_target.st_ino) != (
            target_metadata.st_dev,
            target_metadata.st_ino,
        ):
            raise R25ArtifactBuildError(
                "PATH_IDENTITY_DRIFT", "bundle directory path changed during publication"
            )
    except R25ArtifactBuildError:
        raise
    except OSError as exc:
        raise R25ArtifactBuildError("ARTIFACT_WRITE_FAILED", "artifact write failed") from exc
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        _close_directory_chain(parent_chain)
    assert published_identity is not None
    validation = validate_written_artifact_bundle_v1(
        target,
        repository_root=repository_root,
        expected_source_commit=expected_source_commit,
        allow_unverified_cpu_fixture=allow_unverified_cpu_fixture,
        _expected_bundle_identity=published_identity,
    )
    if validation.artifact_bundle_sha256 != artifact_bundle_sha256(bundle):
        raise R25ArtifactBuildError(
            "ARTIFACT_BUNDLE_MISMATCH", "durable bundle differs from the built projection"
        )
    return tuple(written)


__all__ = [
    "ARTIFACT_BUNDLE_FILENAME",
    "ARTIFACT_BUNDLE_SCHEMA_VERSION",
    "COHORT_SELECTION_ALGORITHM",
    "COHORT_SELECTION_FILENAME",
    "COHORT_SELECTION_SCHEMA_VERSION",
    "GUI_ONLY_TASK_SOURCE_TASK_COUNT",
    "GUI_ONLY_TASK_SOURCE_FREEZE_SCHEMA_VERSION",
    "GUI_ONLY_TASK_SOURCE_TRIAL",
    "GUI_ONLY_TASK_SOURCE_TRIAL_PROVENANCE",
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
    "ArtifactBundleReadbackValidationV1",
    "CohortMemberV1",
    "CohortSelectionV1",
    "FrozenGuiOnlyTaskSourceV1",
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
    "freeze_gui_only_task_source_from_historical_manifest_v1",
    "frozen_gui_only_task_source_projection_v1",
    "frozen_gui_only_task_source_receipt_v1",
    "frozen_gui_only_task_source_sha256_v1",
    "load_gui_only_task_source_freeze_receipt_v1",
    "parse_cohort_selection",
    "select_gui_only_cohort",
    "select_gui_only_cohort_from_bytes",
    "validate_written_artifact_bundle_v1",
    "verify_current_source_commit",
    "write_artifact_bundle",
    "write_fresh_gui_only_task_source_v1",
    "write_fresh_gui_only_task_source_freeze_receipt_v1",
]
