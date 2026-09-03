"""Frozen, matched R2.5 Baseline-versus-Joint pilot plan.

This module deliberately cannot run MobileWorld.  It validates and hashes the
20--30 task cohort and derives the exact matched execution cells that a later,
owner-authorized production executor must consume.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes, canonical_sha256
from mobile_world.runtime.sentinel.r2_4.topology_artifact import (
    parse_r24_cpu_topology_artifact,
    r24_cpu_topology_artifact_sha256,
)

FROZEN_PILOT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.5-frozen-pilot/v1"
PILOT_TASK_SOURCE_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.5-task-source/v1"
EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.5-executable-task-source/v1"
)
RESOLVED_PILOT_TASK_INPUTS_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.5-resolved-task-inputs/v1"
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_UTC_SECOND = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_MAX_TASK_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_TASK_PARAMETERS_BYTES = 1024 * 1024
_RESOLUTION_SEAL = object()


class R25PilotContractError(ValueError):
    """A stable, secret-free R2.5 pilot-plan validation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class PilotHostV1(StrEnum):
    QWEN3_VL = "QWEN3_VL"
    MAI_UI = "MAI_UI"


class PilotArmV1(StrEnum):
    BASELINE = "BASELINE"
    JOINT_SENTINEL = "JOINT_SENTINEL"


class PilotTopologyV1(StrEnum):
    ISOLATED_HISTORY_FREE = "ISOLATED_HISTORY_FREE"


class PilotSeedPolicyV1(StrEnum):
    FIXED_PER_TASK_SHARED_ACROSS_HOSTS_AND_ARMS = "FIXED_PER_TASK_SHARED_ACROSS_HOSTS_AND_ARMS"


class PilotTaskTimeAuthorityV1(StrEnum):
    STATIC_WALL_CLOCK_INDEPENDENT_ONLY = "STATIC_WALL_CLOCK_INDEPENDENT_ONLY"


class PilotTaskParameterSourceKindV1(StrEnum):
    INLINE_CANONICAL_JSON = "INLINE_CANONICAL_JSON"
    EXTERNAL_CANONICAL_JSON_BLOB = "EXTERNAL_CANONICAL_JSON_BLOB"


def _require_id(value: object, name: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise R25PilotContractError("INVALID_ID", f"{name} is invalid")
    return value


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise R25PilotContractError("INVALID_SHA256", f"{name} is invalid")
    return value


def _require_positive_int(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise R25PilotContractError("INVALID_BOUND", f"{name} is outside its closed bound")
    return value


def _require_exact_keys(value: object, expected: frozenset[str], name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise R25PilotContractError("UNTRUSTED_TYPE", f"{name} must be an object")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != expected:
        raise R25PilotContractError("INVALID_FIELDS", f"{name} fields do not match the contract")
    return cast(dict[str, object], mapping)


def _require_absolute_path(value: object, name: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise R25PilotContractError("INVALID_PATH", f"{name} is invalid")
    path = Path(value)
    if not path.is_absolute() or len(value) > 4096:
        raise R25PilotContractError("INVALID_PATH", f"{name} must be a bounded absolute path")
    return value


@dataclass(frozen=True, slots=True)
class PilotTaskV1:
    task_id: str
    task_parameters_sha256: str
    reset_seed: int

    def __post_init__(self) -> None:
        _require_id(self.task_id, "task_id")
        _require_sha256(self.task_parameters_sha256, "task_parameters_sha256")
        if type(self.reset_seed) is not int or not 0 <= self.reset_seed <= 2_147_483_647:
            raise R25PilotContractError("INVALID_SEED", "reset_seed is outside int32")


@dataclass(frozen=True, slots=True)
class MobileWorldTaskParametersV1:
    """Closed task-definition payload consumed by the R2.5 MobileWorld driver."""

    task_name: str
    trial: int

    def __post_init__(self) -> None:
        _require_id(self.task_name, "task_name")
        _require_positive_int(self.trial, "trial", 1_000_000)


@dataclass(frozen=True, slots=True)
class InlinePilotTaskParametersV1:
    task_id: str
    parameters: MobileWorldTaskParametersV1

    def __post_init__(self) -> None:
        _require_id(self.task_id, "task_id")
        if type(self.parameters) is not MobileWorldTaskParametersV1:
            raise R25PilotContractError(
                "UNTRUSTED_TYPE", "inline parameters must use MobileWorldTaskParametersV1"
            )
        if self.parameters.task_name != self.task_id:
            raise R25PilotContractError(
                "TASK_BINDING_MISMATCH", "inline task_name does not equal task_id"
            )


@dataclass(frozen=True, slots=True)
class ExternalPilotTaskParametersV1:
    task_id: str
    path: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _require_id(self.task_id, "task_id")
        _require_absolute_path(self.path, "task parameter blob path")
        _require_sha256(self.sha256, "task parameter blob sha256")
        _require_positive_int(
            self.byte_count, "task parameter blob byte_count", _MAX_TASK_PARAMETERS_BYTES
        )


type PilotTaskParameterBindingV1 = InlinePilotTaskParametersV1 | ExternalPilotTaskParametersV1


@dataclass(frozen=True, slots=True)
class PilotResetTaskInitInputV1:
    """Detached reset and task-init values for one matched execution cell.

    A production driver must pass and validate all four execution bindings:
    task name, trial, parameter hash, and reset seed.  Dropping a binding or
    substituting a host/arm-local seed is a contract failure.
    """

    task_id: str
    task_name: str
    trial: int
    task_parameters_sha256: str
    reset_seed: int
    seed_policy: PilotSeedPolicyV1
    parameter_source_kind: PilotTaskParameterSourceKindV1
    task_time_authority: PilotTaskTimeAuthorityV1
    cohort_selection_sha256: str

    def __post_init__(self) -> None:
        PilotTaskV1(self.task_id, self.task_parameters_sha256, self.reset_seed)
        MobileWorldTaskParametersV1(self.task_name, self.trial)
        if self.task_name != self.task_id:
            raise R25PilotContractError(
                "TASK_BINDING_MISMATCH", "resolved task_name does not equal task_id"
            )
        if type(self.seed_policy) is not PilotSeedPolicyV1:
            raise R25PilotContractError("UNTRUSTED_TYPE", "seed policy is untrusted")
        if type(self.parameter_source_kind) is not PilotTaskParameterSourceKindV1:
            raise R25PilotContractError("UNTRUSTED_TYPE", "parameter source kind is untrusted")
        if (
            type(self.task_time_authority) is not PilotTaskTimeAuthorityV1
            or self.task_time_authority
            is not PilotTaskTimeAuthorityV1.STATIC_WALL_CLOCK_INDEPENDENT_ONLY
        ):
            raise R25PilotContractError(
                "DYNAMIC_TASK_TIME_FORBIDDEN",
                "pilot reset input must be statically wall-clock independent",
            )
        _require_sha256(self.cohort_selection_sha256, "cohort_selection_sha256")

    @property
    def environment_reset_input(self) -> dict[str, JsonValue]:
        return {
            "reset_seed": self.reset_seed,
            "seed_policy": self.seed_policy.value,
            "task_time_authority": self.task_time_authority.value,
            "cohort_selection_sha256": self.cohort_selection_sha256,
            "task_id": self.task_id,
        }

    @property
    def task_initialization_input(self) -> dict[str, JsonValue]:
        return {"task_name": self.task_name, "trial": self.trial}


@dataclass(frozen=True, slots=True)
class ResolvedPilotTaskInputsV1:
    """Module-issued proof that every executable pilot input was revalidated."""

    schema_version: str
    cohort_id: str
    frozen_pilot_manifest_sha256: str
    task_source_sha256: str
    task_source_byte_count: int
    seed_policy: PilotSeedPolicyV1
    task_time_authority: PilotTaskTimeAuthorityV1
    cohort_selection_sha256: str
    tasks: tuple[PilotResetTaskInitInputV1, ...]
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _RESOLUTION_SEAL:
            raise R25PilotContractError(
                "UNTRUSTED_RESOLUTION", "resolved inputs must be issued by the module resolver"
            )
        if self.schema_version != RESOLVED_PILOT_TASK_INPUTS_SCHEMA_VERSION:
            raise R25PilotContractError("UNKNOWN_SCHEMA", "unknown resolution schema")
        _require_id(self.cohort_id, "cohort_id")
        _require_sha256(self.frozen_pilot_manifest_sha256, "frozen manifest sha256")
        _require_sha256(self.task_source_sha256, "task source sha256")
        _require_positive_int(
            self.task_source_byte_count, "task source byte_count", _MAX_TASK_SOURCE_BYTES
        )
        if type(self.seed_policy) is not PilotSeedPolicyV1:
            raise R25PilotContractError("UNTRUSTED_TYPE", "seed policy is untrusted")
        if (
            type(self.task_time_authority) is not PilotTaskTimeAuthorityV1
            or self.task_time_authority
            is not PilotTaskTimeAuthorityV1.STATIC_WALL_CLOCK_INDEPENDENT_ONLY
        ):
            raise R25PilotContractError(
                "DYNAMIC_TASK_TIME_FORBIDDEN",
                "resolved inputs exceed the static-time pilot authority",
            )
        _require_sha256(self.cohort_selection_sha256, "cohort_selection_sha256")
        if type(self.tasks) is not tuple or not 20 <= len(self.tasks) <= 30:
            raise R25PilotContractError("INVALID_COHORT_SIZE", "resolved inputs need 20--30 tasks")
        if any(type(task) is not PilotResetTaskInitInputV1 for task in self.tasks):
            raise R25PilotContractError("UNTRUSTED_TYPE", "resolved task input is untrusted")
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise R25PilotContractError("DUPLICATE_TASK", "resolved task IDs must be unique")


@dataclass(frozen=True, slots=True)
class PilotCellV1:
    task_id: str
    task_parameters_sha256: str
    reset_seed: int
    host: PilotHostV1
    arm: PilotArmV1
    sentinel_mode: str

    def __post_init__(self) -> None:
        PilotTaskV1(self.task_id, self.task_parameters_sha256, self.reset_seed)
        if type(self.host) is not PilotHostV1 or type(self.arm) is not PilotArmV1:
            raise R25PilotContractError("UNTRUSTED_TYPE", "pilot cell enums are untrusted")
        expected = "OFF" if self.arm is PilotArmV1.BASELINE else "ACTIVE"
        if type(self.sentinel_mode) is not str or self.sentinel_mode != expected:
            raise R25PilotContractError("ARM_MODE_MISMATCH", "pilot arm has the wrong mode")


@dataclass(frozen=True, slots=True)
class FrozenPilotManifestV1:
    schema_version: str
    cohort_id: str
    frozen_at_utc: str
    task_manifest_path: str
    task_manifest_sha256: str
    task_manifest_byte_count: int
    topology_comparison_artifact_path: str
    topology_comparison_artifact_sha256: str
    topology_comparison_artifact_byte_count: int
    cohort_selection_artifact_path: str
    cohort_selection_artifact_sha256: str
    cohort_selection_artifact_byte_count: int
    cohort_selection_sha256: str
    task_time_authority: PilotTaskTimeAuthorityV1
    dynamic_wall_clock_tasks_excluded: bool
    tasks: tuple[PilotTaskV1, ...]
    hosts: tuple[PilotHostV1, ...]
    arms: tuple[PilotArmV1, ...]
    topology: PilotTopologyV1
    seed_policy: PilotSeedPolicyV1
    baseline_mode: str
    joint_mode: str
    environment_reset_between_cells: bool
    matched_task_ids: bool
    matched_task_parameters: bool
    official_success_metric_required: bool
    max_steps_per_cell: int
    per_cell_timeout_seconds: int
    max_total_wall_time_seconds: int
    max_total_actor_calls: int
    max_total_openai_calls: int
    max_total_cost_usd_micros: int

    def __post_init__(self) -> None:
        if self.schema_version != FROZEN_PILOT_SCHEMA_VERSION:
            raise R25PilotContractError("UNKNOWN_SCHEMA", "unknown frozen-pilot schema")
        _require_id(self.cohort_id, "cohort_id")
        if type(self.frozen_at_utc) is not str or _UTC_SECOND.fullmatch(self.frozen_at_utc) is None:
            raise R25PilotContractError("INVALID_TIMESTAMP", "frozen_at_utc must be UTC to seconds")
        _require_absolute_path(self.task_manifest_path, "task_manifest_path")
        _require_sha256(self.task_manifest_sha256, "task_manifest_sha256")
        _require_positive_int(
            self.task_manifest_byte_count, "task_manifest_byte_count", 100_000_000
        )
        _require_absolute_path(
            self.topology_comparison_artifact_path,
            "topology_comparison_artifact_path",
        )
        _require_sha256(
            self.topology_comparison_artifact_sha256,
            "topology_comparison_artifact_sha256",
        )
        _require_positive_int(
            self.topology_comparison_artifact_byte_count,
            "topology_comparison_artifact_byte_count",
            8 * 1024 * 1024,
        )
        _require_absolute_path(
            self.cohort_selection_artifact_path,
            "cohort_selection_artifact_path",
        )
        _require_sha256(
            self.cohort_selection_artifact_sha256,
            "cohort_selection_artifact_sha256",
        )
        _require_positive_int(
            self.cohort_selection_artifact_byte_count,
            "cohort_selection_artifact_byte_count",
            8 * 1024 * 1024,
        )
        _require_sha256(self.cohort_selection_sha256, "cohort_selection_sha256")
        if self.cohort_selection_sha256 != self.cohort_selection_artifact_sha256:
            raise R25PilotContractError(
                "COHORT_SELECTION_BINDING_MISMATCH",
                "cohort selection digest must bind its exact artifact bytes",
            )
        if (
            type(self.task_time_authority) is not PilotTaskTimeAuthorityV1
            or self.task_time_authority  # type: ignore[redundant-expr]
            is not PilotTaskTimeAuthorityV1.STATIC_WALL_CLOCK_INDEPENDENT_ONLY
            or type(self.dynamic_wall_clock_tasks_excluded) is not bool  # type: ignore[redundant-expr]
            or self.dynamic_wall_clock_tasks_excluded is not True
        ):
            raise R25PilotContractError(
                "DYNAMIC_TASK_TIME_FORBIDDEN",
                "pilot admits only statically wall-clock-independent tasks",
            )
        if type(self.tasks) is not tuple or not 20 <= len(self.tasks) <= 30:
            raise R25PilotContractError("INVALID_COHORT_SIZE", "pilot needs 20--30 exact tasks")
        if any(type(task) is not PilotTaskV1 for task in self.tasks):
            raise R25PilotContractError("UNTRUSTED_TYPE", "tasks must use exact PilotTaskV1 values")
        task_ids = tuple(task.task_id for task in self.tasks)
        if len(set(task_ids)) != len(task_ids):
            raise R25PilotContractError("DUPLICATE_TASK", "pilot task IDs must be unique")
        if self.hosts != (PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI):
            raise R25PilotContractError("INVALID_HOST_MATRIX", "pilot needs Qwen then MAI")
        if self.arms != (PilotArmV1.BASELINE, PilotArmV1.JOINT_SENTINEL):
            raise R25PilotContractError("INVALID_ARM_MATRIX", "pilot needs Baseline then Joint")
        if type(self.topology) is not PilotTopologyV1:
            raise R25PilotContractError("UNTRUSTED_TYPE", "pilot topology is untrusted")
        if self.topology is not PilotTopologyV1.ISOLATED_HISTORY_FREE:
            raise R25PilotContractError("INVALID_TOPOLOGY", "pilot must use isolated grounding")
        if type(self.seed_policy) is not PilotSeedPolicyV1:
            raise R25PilotContractError("UNTRUSTED_TYPE", "seed policy is untrusted")
        if self.baseline_mode != "OFF" or self.joint_mode != "ACTIVE":
            raise R25PilotContractError("ARM_MODE_MISMATCH", "pilot modes must be OFF/ACTIVE")
        required_true = (
            self.environment_reset_between_cells,
            self.matched_task_ids,
            self.matched_task_parameters,
            self.official_success_metric_required,
        )
        if any(value is not True for value in required_true):
            raise R25PilotContractError(
                "MATCHING_REQUIRED", "pilot matching/reset/accounting is required"
            )
        cell_count = len(self.tasks) * len(self.hosts) * len(self.arms)
        _require_positive_int(self.max_steps_per_cell, "max_steps_per_cell", 200)
        _require_positive_int(self.per_cell_timeout_seconds, "per_cell_timeout_seconds", 14_400)
        _require_positive_int(
            self.max_total_wall_time_seconds, "max_total_wall_time_seconds", 604_800
        )
        if self.max_total_wall_time_seconds < self.per_cell_timeout_seconds:
            raise R25PilotContractError("INVALID_BOUND", "total time is below one cell timeout")
        _require_positive_int(self.max_total_actor_calls, "max_total_actor_calls", 1_000_000)
        if self.max_total_actor_calls < cell_count:
            raise R25PilotContractError(
                "INVALID_BOUND", "actor-call cap cannot cover every matched cell"
            )
        if self.max_total_actor_calls > cell_count * self.max_steps_per_cell:
            raise R25PilotContractError("INVALID_BOUND", "actor-call cap exceeds the cell/step cap")
        _require_positive_int(self.max_total_openai_calls, "max_total_openai_calls", 2_000_000)
        baseline_cell_count = len(self.tasks) * len(self.hosts)
        # The isolated pilot may spend one task-start rubric generation per
        # Joint-Sentinel cell, then one history-free rubric tracking call and
        # one history-policy call per Joint-Sentinel actor decision.  Baseline
        # cells consume at least one actor call apiece and make no OpenAI call.
        max_isolated_openai_calls = (
            2 * (self.max_total_actor_calls - baseline_cell_count) + baseline_cell_count
        )
        if self.max_total_openai_calls > max_isolated_openai_calls:
            raise R25PilotContractError(
                "INVALID_BOUND",
                "OpenAI-call cap exceeds isolated rubric plus history-policy bounds",
            )
        _require_positive_int(
            self.max_total_cost_usd_micros, "max_total_cost_usd_micros", 100_000_000_000
        )

    @property
    def cells(self) -> tuple[PilotCellV1, ...]:
        """Derive all matched cells; no mutable caller-owned list is retained."""

        return tuple(
            PilotCellV1(
                task_id=task.task_id,
                task_parameters_sha256=task.task_parameters_sha256,
                reset_seed=task.reset_seed,
                host=host,
                arm=arm,
                sentinel_mode="OFF" if arm is PilotArmV1.BASELINE else "ACTIVE",
            )
            for task in self.tasks
            for host in self.hosts
            for arm in self.arms
        )


def _task_projection(value: PilotTaskV1) -> dict[str, JsonValue]:
    if type(value) is not PilotTaskV1:
        raise R25PilotContractError("UNTRUSTED_TYPE", "task must use exact PilotTaskV1")
    value = PilotTaskV1(
        task_id=value.task_id,
        task_parameters_sha256=value.task_parameters_sha256,
        reset_seed=value.reset_seed,
    )
    return {
        "reset_seed": value.reset_seed,
        "task_id": value.task_id,
        "task_parameters_sha256": value.task_parameters_sha256,
    }


def frozen_pilot_manifest_projection(value: FrozenPilotManifestV1) -> dict[str, JsonValue]:
    if type(value) is not FrozenPilotManifestV1:
        raise R25PilotContractError("UNTRUSTED_TYPE", "manifest must use exact frozen type")
    # Rebuild to re-run all invariants if a frozen instance was tampered with.
    trusted = FrozenPilotManifestV1(
        **{field.name: getattr(value, field.name) for field in fields(FrozenPilotManifestV1)}
    )
    return {
        "arms": [arm.value for arm in trusted.arms],
        "baseline_mode": trusted.baseline_mode,
        "cohort_id": trusted.cohort_id,
        "cohort_selection_artifact_byte_count": trusted.cohort_selection_artifact_byte_count,
        "cohort_selection_artifact_path": trusted.cohort_selection_artifact_path,
        "cohort_selection_artifact_sha256": trusted.cohort_selection_artifact_sha256,
        "cohort_selection_sha256": trusted.cohort_selection_sha256,
        "dynamic_wall_clock_tasks_excluded": trusted.dynamic_wall_clock_tasks_excluded,
        "environment_reset_between_cells": trusted.environment_reset_between_cells,
        "frozen_at_utc": trusted.frozen_at_utc,
        "hosts": [host.value for host in trusted.hosts],
        "joint_mode": trusted.joint_mode,
        "matched_task_ids": trusted.matched_task_ids,
        "matched_task_parameters": trusted.matched_task_parameters,
        "max_steps_per_cell": trusted.max_steps_per_cell,
        "max_total_actor_calls": trusted.max_total_actor_calls,
        "max_total_cost_usd_micros": trusted.max_total_cost_usd_micros,
        "max_total_openai_calls": trusted.max_total_openai_calls,
        "max_total_wall_time_seconds": trusted.max_total_wall_time_seconds,
        "official_success_metric_required": trusted.official_success_metric_required,
        "per_cell_timeout_seconds": trusted.per_cell_timeout_seconds,
        "schema_version": trusted.schema_version,
        "seed_policy": trusted.seed_policy.value,
        "task_manifest_byte_count": trusted.task_manifest_byte_count,
        "task_manifest_path": trusted.task_manifest_path,
        "task_manifest_sha256": trusted.task_manifest_sha256,
        "task_time_authority": trusted.task_time_authority.value,
        "tasks": [cast(JsonValue, _task_projection(task)) for task in trusted.tasks],
        "topology": trusted.topology.value,
        "topology_comparison_artifact_byte_count": (
            trusted.topology_comparison_artifact_byte_count
        ),
        "topology_comparison_artifact_path": trusted.topology_comparison_artifact_path,
        "topology_comparison_artifact_sha256": trusted.topology_comparison_artifact_sha256,
    }


def frozen_pilot_manifest_sha256(value: FrozenPilotManifestV1) -> str:
    return canonical_sha256(cast(JsonValue, frozen_pilot_manifest_projection(value)))


def pilot_task_source_projection(
    cohort_id: str, tasks: tuple[PilotTaskV1, ...]
) -> dict[str, JsonValue]:
    """Legacy hash-only CPU-planning source; never executable in production.

    R2.4 CPU fixtures predate the executable task-parameter contract.  Keeping
    this projection avoids silently reinterpreting those fixtures.  Production
    resolution accepts only :data:`EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION`.
    """

    _require_id(cohort_id, "cohort_id")
    if type(tasks) is not tuple or not 20 <= len(tasks) <= 30:
        raise R25PilotContractError("INVALID_COHORT_SIZE", "task source needs 20--30 tasks")
    if any(type(task) is not PilotTaskV1 for task in tasks):
        raise R25PilotContractError("UNTRUSTED_TYPE", "task source contains an untrusted task")
    if len({task.task_id for task in tasks}) != len(tasks):
        raise R25PilotContractError("DUPLICATE_TASK", "task source IDs must be unique")
    return {
        "cohort_id": cohort_id,
        "schema_version": PILOT_TASK_SOURCE_SCHEMA_VERSION,
        "tasks": [cast(JsonValue, _task_projection(task)) for task in tasks],
    }


def _mobileworld_task_parameters_projection(
    value: MobileWorldTaskParametersV1,
) -> dict[str, JsonValue]:
    if type(value) is not MobileWorldTaskParametersV1:
        raise R25PilotContractError(
            "UNTRUSTED_TYPE", "parameters must use exact MobileWorldTaskParametersV1"
        )
    trusted = MobileWorldTaskParametersV1(task_name=value.task_name, trial=value.trial)
    return {"task_name": trusted.task_name, "trial": trusted.trial}


def executable_pilot_task_source_projection(
    cohort_id: str,
    tasks: tuple[PilotTaskV1, ...],
    bindings: tuple[PilotTaskParameterBindingV1, ...],
    *,
    seed_policy: PilotSeedPolicyV1 = (
        PilotSeedPolicyV1.FIXED_PER_TASK_SHARED_ACROSS_HOSTS_AND_ARMS
    ),
) -> dict[str, JsonValue]:
    """Build the only task source that the production resolver will accept."""

    # Reuse the complete task/cohort validation without treating its v1
    # projection as executable authority.
    pilot_task_source_projection(cohort_id, tasks)
    if type(seed_policy) is not PilotSeedPolicyV1:
        raise R25PilotContractError("UNTRUSTED_TYPE", "seed policy is untrusted")
    if type(bindings) is not tuple or len(bindings) != len(tasks):
        raise R25PilotContractError(
            "TASK_BINDING_MISMATCH", "parameter bindings must match every task exactly"
        )

    projected_tasks: list[JsonValue] = []
    for task, binding in zip(tasks, bindings, strict=True):
        if type(binding) is InlinePilotTaskParametersV1:
            trusted_inline = InlinePilotTaskParametersV1(
                task_id=binding.task_id,
                parameters=MobileWorldTaskParametersV1(
                    task_name=binding.parameters.task_name,
                    trial=binding.parameters.trial,
                ),
            )
            payload = _mobileworld_task_parameters_projection(trusted_inline.parameters)
            payload_bytes = canonical_json_bytes(cast(JsonValue, payload))
            payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
            if (
                trusted_inline.task_id != task.task_id
                or payload_sha256 != task.task_parameters_sha256
            ):
                raise R25PilotContractError(
                    "TASK_BINDING_MISMATCH",
                    "inline parameters do not match the frozen task identity/hash",
                )
            parameter_source: dict[str, JsonValue] = {
                "byte_count": len(payload_bytes),
                "kind": PilotTaskParameterSourceKindV1.INLINE_CANONICAL_JSON.value,
                "payload": cast(JsonValue, payload),
                "sha256": payload_sha256,
            }
        elif type(binding) is ExternalPilotTaskParametersV1:
            trusted_external = ExternalPilotTaskParametersV1(
                task_id=binding.task_id,
                path=binding.path,
                sha256=binding.sha256,
                byte_count=binding.byte_count,
            )
            if (
                trusted_external.task_id != task.task_id
                or trusted_external.sha256 != task.task_parameters_sha256
            ):
                raise R25PilotContractError(
                    "TASK_BINDING_MISMATCH",
                    "external parameters do not match the frozen task identity/hash",
                )
            parameter_source = {
                "byte_count": trusted_external.byte_count,
                "kind": PilotTaskParameterSourceKindV1.EXTERNAL_CANONICAL_JSON_BLOB.value,
                "path": trusted_external.path,
                "sha256": trusted_external.sha256,
            }
        else:
            raise R25PilotContractError(
                "UNTRUSTED_TYPE", "parameter binding uses an unknown runtime type"
            )
        projected_tasks.append(
            {
                "parameter_source": parameter_source,
                "reset_seed": task.reset_seed,
                "task_id": task.task_id,
                "task_parameters_sha256": task.task_parameters_sha256,
            }
        )

    return {
        "cohort_id": cohort_id,
        "schema_version": EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION,
        "seed_policy": seed_policy.value,
        "tasks": projected_tasks,
    }


def _resolution_task_projection(value: PilotResetTaskInitInputV1) -> dict[str, JsonValue]:
    if type(value) is not PilotResetTaskInitInputV1:
        raise R25PilotContractError("UNTRUSTED_TYPE", "resolved task input is untrusted")
    trusted = PilotResetTaskInitInputV1(
        task_id=value.task_id,
        task_name=value.task_name,
        trial=value.trial,
        task_parameters_sha256=value.task_parameters_sha256,
        reset_seed=value.reset_seed,
        seed_policy=value.seed_policy,
        parameter_source_kind=value.parameter_source_kind,
        task_time_authority=value.task_time_authority,
        cohort_selection_sha256=value.cohort_selection_sha256,
    )
    return {
        "parameter_source_kind": trusted.parameter_source_kind.value,
        "cohort_selection_sha256": trusted.cohort_selection_sha256,
        "reset_seed": trusted.reset_seed,
        "seed_policy": trusted.seed_policy.value,
        "task_id": trusted.task_id,
        "task_name": trusted.task_name,
        "task_parameters_sha256": trusted.task_parameters_sha256,
        "task_time_authority": trusted.task_time_authority.value,
        "trial": trusted.trial,
    }


def resolved_pilot_task_inputs_projection(
    value: ResolvedPilotTaskInputsV1,
) -> dict[str, JsonValue]:
    if type(value) is not ResolvedPilotTaskInputsV1 or value._seal is not _RESOLUTION_SEAL:
        raise R25PilotContractError("UNTRUSTED_RESOLUTION", "resolution is not module-issued")
    trusted = ResolvedPilotTaskInputsV1(
        schema_version=value.schema_version,
        cohort_id=value.cohort_id,
        frozen_pilot_manifest_sha256=value.frozen_pilot_manifest_sha256,
        task_source_sha256=value.task_source_sha256,
        task_source_byte_count=value.task_source_byte_count,
        seed_policy=value.seed_policy,
        task_time_authority=value.task_time_authority,
        cohort_selection_sha256=value.cohort_selection_sha256,
        tasks=tuple(value.tasks),
        _seal=_RESOLUTION_SEAL,
    )
    return {
        "cohort_id": trusted.cohort_id,
        "cohort_selection_sha256": trusted.cohort_selection_sha256,
        "frozen_pilot_manifest_sha256": trusted.frozen_pilot_manifest_sha256,
        "schema_version": trusted.schema_version,
        "seed_policy": trusted.seed_policy.value,
        "task_source_byte_count": trusted.task_source_byte_count,
        "task_source_sha256": trusted.task_source_sha256,
        "task_time_authority": trusted.task_time_authority.value,
        "tasks": [cast(JsonValue, _resolution_task_projection(task)) for task in trusted.tasks],
    }


def resolved_pilot_task_inputs_sha256(value: ResolvedPilotTaskInputsV1) -> str:
    return canonical_sha256(cast(JsonValue, resolved_pilot_task_inputs_projection(value)))


_PILOT_FIELDS = frozenset(
    {
        "arms",
        "baseline_mode",
        "cohort_id",
        "cohort_selection_artifact_byte_count",
        "cohort_selection_artifact_path",
        "cohort_selection_artifact_sha256",
        "cohort_selection_sha256",
        "dynamic_wall_clock_tasks_excluded",
        "environment_reset_between_cells",
        "frozen_at_utc",
        "hosts",
        "joint_mode",
        "matched_task_ids",
        "matched_task_parameters",
        "max_steps_per_cell",
        "max_total_actor_calls",
        "max_total_cost_usd_micros",
        "max_total_openai_calls",
        "max_total_wall_time_seconds",
        "official_success_metric_required",
        "per_cell_timeout_seconds",
        "schema_version",
        "seed_policy",
        "task_manifest_byte_count",
        "task_manifest_path",
        "task_manifest_sha256",
        "task_time_authority",
        "tasks",
        "topology",
        "topology_comparison_artifact_byte_count",
        "topology_comparison_artifact_path",
        "topology_comparison_artifact_sha256",
    }
)
_TASK_FIELDS = frozenset({"reset_seed", "task_id", "task_parameters_sha256"})


def parse_frozen_pilot_manifest(value: object) -> FrozenPilotManifestV1:
    mapping = _require_exact_keys(value, _PILOT_FIELDS, "pilot")
    raw_tasks = mapping["tasks"]
    if type(raw_tasks) is not list or not 20 <= len(raw_tasks) <= 30:
        raise R25PilotContractError("INVALID_COHORT_SIZE", "pilot needs 20--30 task objects")
    tasks: list[PilotTaskV1] = []
    for item in cast(list[object], raw_tasks):
        task = _require_exact_keys(item, _TASK_FIELDS, "pilot task")
        tasks.append(
            PilotTaskV1(
                task_id=cast(str, task["task_id"]),
                task_parameters_sha256=cast(str, task["task_parameters_sha256"]),
                reset_seed=cast(int, task["reset_seed"]),
            )
        )
    raw_hosts = mapping["hosts"]
    raw_arms = mapping["arms"]
    if type(raw_hosts) is not list or type(raw_arms) is not list:
        raise R25PilotContractError("UNTRUSTED_TYPE", "hosts and arms must be arrays")
    try:
        hosts = tuple(PilotHostV1(cast(str, item)) for item in cast(list[object], raw_hosts))
        arms = tuple(PilotArmV1(cast(str, item)) for item in cast(list[object], raw_arms))
        topology = PilotTopologyV1(cast(str, mapping["topology"]))
        seed_policy = PilotSeedPolicyV1(cast(str, mapping["seed_policy"]))
        task_time_authority = PilotTaskTimeAuthorityV1(cast(str, mapping["task_time_authority"]))
    except (TypeError, ValueError) as exc:
        raise R25PilotContractError("INVALID_ENUM", "pilot contains an unknown enum") from exc
    return FrozenPilotManifestV1(
        schema_version=cast(str, mapping["schema_version"]),
        cohort_id=cast(str, mapping["cohort_id"]),
        frozen_at_utc=cast(str, mapping["frozen_at_utc"]),
        task_manifest_path=cast(str, mapping["task_manifest_path"]),
        task_manifest_sha256=cast(str, mapping["task_manifest_sha256"]),
        task_manifest_byte_count=cast(int, mapping["task_manifest_byte_count"]),
        topology_comparison_artifact_path=cast(str, mapping["topology_comparison_artifact_path"]),
        topology_comparison_artifact_sha256=cast(
            str, mapping["topology_comparison_artifact_sha256"]
        ),
        topology_comparison_artifact_byte_count=cast(
            int, mapping["topology_comparison_artifact_byte_count"]
        ),
        cohort_selection_artifact_path=cast(str, mapping["cohort_selection_artifact_path"]),
        cohort_selection_artifact_sha256=cast(str, mapping["cohort_selection_artifact_sha256"]),
        cohort_selection_artifact_byte_count=cast(
            int, mapping["cohort_selection_artifact_byte_count"]
        ),
        cohort_selection_sha256=cast(str, mapping["cohort_selection_sha256"]),
        task_time_authority=task_time_authority,
        dynamic_wall_clock_tasks_excluded=cast(bool, mapping["dynamic_wall_clock_tasks_excluded"]),
        tasks=tuple(tasks),
        hosts=hosts,
        arms=arms,
        topology=topology,
        seed_policy=seed_policy,
        baseline_mode=cast(str, mapping["baseline_mode"]),
        joint_mode=cast(str, mapping["joint_mode"]),
        environment_reset_between_cells=cast(bool, mapping["environment_reset_between_cells"]),
        matched_task_ids=cast(bool, mapping["matched_task_ids"]),
        matched_task_parameters=cast(bool, mapping["matched_task_parameters"]),
        official_success_metric_required=cast(bool, mapping["official_success_metric_required"]),
        max_steps_per_cell=cast(int, mapping["max_steps_per_cell"]),
        per_cell_timeout_seconds=cast(int, mapping["per_cell_timeout_seconds"]),
        max_total_wall_time_seconds=cast(int, mapping["max_total_wall_time_seconds"]),
        max_total_actor_calls=cast(int, mapping["max_total_actor_calls"]),
        max_total_openai_calls=cast(int, mapping["max_total_openai_calls"]),
        max_total_cost_usd_micros=cast(int, mapping["max_total_cost_usd_micros"]),
    )


_EXECUTABLE_SOURCE_FIELDS = frozenset({"cohort_id", "schema_version", "seed_policy", "tasks"})
_EXECUTABLE_TASK_FIELDS = frozenset(
    {"parameter_source", "reset_seed", "task_id", "task_parameters_sha256"}
)
_INLINE_SOURCE_FIELDS = frozenset({"byte_count", "kind", "payload", "sha256"})
_EXTERNAL_SOURCE_FIELDS = frozenset({"byte_count", "kind", "path", "sha256"})
_MOBILEWORLD_PARAMETER_FIELDS = frozenset({"task_name", "trial"})


def _reject_duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise R25PilotContractError("NONCANONICAL_JSON", "JSON object contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    del value
    raise R25PilotContractError("NONCANONICAL_JSON", "non-finite JSON number is forbidden")


def _require_bounded_json_tree(value: object, name: str) -> JsonValue:
    stack: list[tuple[object, int]] = [(value, 1)]
    visits = 0
    while stack:
        current, depth = stack.pop()
        visits += 1
        if depth > 32 or visits > 100_000:
            raise R25PilotContractError("NONCANONICAL_JSON", f"{name} exceeds graph bounds")
        if current is None or type(current) in {bool, int, str}:
            continue
        if type(current) is list:
            stack.extend((item, depth + 1) for item in cast(list[object], current))
            continue
        if type(current) is dict:
            mapping = cast(dict[object, object], current)
            if any(type(key) is not str for key in mapping):
                raise R25PilotContractError(
                    "NONCANONICAL_JSON", f"{name} contains a non-string key"
                )
            stack.extend((item, depth + 1) for item in mapping.values())
            continue
        # Floats and every serializer-coercible Python value are deliberately
        # excluded from this small MobileWorld task-definition format.
        raise R25PilotContractError("NONCANONICAL_JSON", f"{name} has an unsupported value")
    return cast(JsonValue, value)


def _decode_canonical_json(raw: bytes, name: str) -> JsonValue:
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_json_constant,
        )
        trusted = _require_bounded_json_tree(decoded, name)
        if canonical_json_bytes(trusted) != raw:
            raise R25PilotContractError(
                "NONCANONICAL_JSON", f"{name} is not exact canonical JSON bytes"
            )
        return trusted
    except R25PilotContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise R25PilotContractError("NONCANONICAL_JSON", f"{name} is invalid JSON") from exc


def _path_under_authorized_root(path: Path, root: Path, name: str) -> tuple[Path, tuple[str, ...]]:
    if not path.is_absolute() or not root.is_absolute() or ".." in path.parts or ".." in root.parts:
        raise R25PilotContractError(
            "TASK_SOURCE_OUTSIDE_ROOT", f"{name} is not a canonical absolute child path"
        )
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise R25PilotContractError(
            "TASK_SOURCE_OUTSIDE_ROOT", f"{name} is outside the authorized input root"
        ) from exc
    if not relative.parts:
        raise R25PilotContractError("INVALID_PATH", f"{name} must name a file below the root")
    return path, relative.parts


def _require_repo_external_input_root(root: Path, repository_root: Path) -> None:
    _require_absolute_path(str(root), "authorized input root")
    _require_absolute_path(str(repository_root), "repository root")
    if ".." in root.parts or ".." in repository_root.parts:
        raise R25PilotContractError(
            "TASK_SOURCE_OUTSIDE_ROOT", "input and repository roots must be canonical paths"
        )
    try:
        root_info = root.lstat()
        repository_info = repository_root.lstat()
    except FileNotFoundError as exc:
        raise R25PilotContractError(
            "TASK_SOURCE_MISSING", "input or repository root is missing"
        ) from exc
    except OSError as exc:
        raise R25PilotContractError(
            "TASK_SOURCE_UNREADABLE", "cannot inspect input or repository root"
        ) from exc
    if stat.S_ISLNK(root_info.st_mode) or stat.S_ISLNK(repository_info.st_mode):
        raise R25PilotContractError("TASK_SOURCE_SYMLINK", "input/repository root is a symlink")
    if not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISDIR(repository_info.st_mode):
        raise R25PilotContractError("INVALID_PATH", "input/repository root is not a directory")
    try:
        root_real = root.resolve(strict=True)
        repository_real = repository_root.resolve(strict=True)
    except OSError as exc:
        raise R25PilotContractError(
            "TASK_SOURCE_UNREADABLE", "cannot resolve input or repository root"
        ) from exc
    if (
        root_real == repository_real
        or root_real.is_relative_to(repository_real)
        or repository_real.is_relative_to(root_real)
    ):
        raise R25PilotContractError(
            "TASK_SOURCE_INSIDE_REPOSITORY",
            "authorized input root must not overlap the repository root",
        )


def _read_authorized_file(
    declared_path: str,
    *,
    authorized_input_root: Path,
    expected_sha256: str,
    expected_byte_count: int,
    maximum_byte_count: int,
    name: str,
) -> bytes:
    _require_absolute_path(declared_path, name)
    _require_sha256(expected_sha256, f"{name} sha256")
    _require_positive_int(expected_byte_count, f"{name} byte_count", maximum_byte_count)

    root = authorized_input_root
    candidate, relative_parts = _path_under_authorized_root(Path(declared_path), root, name)
    try:
        root_info = root.lstat()
    except FileNotFoundError as exc:
        raise R25PilotContractError(
            "TASK_SOURCE_MISSING", "authorized input root does not exist"
        ) from exc
    except OSError as exc:
        raise R25PilotContractError("TASK_SOURCE_UNREADABLE", "cannot inspect input root") from exc
    if stat.S_ISLNK(root_info.st_mode):
        raise R25PilotContractError("TASK_SOURCE_SYMLINK", "authorized input root is a symlink")
    if not stat.S_ISDIR(root_info.st_mode):
        raise R25PilotContractError("INVALID_PATH", "authorized input root is not a directory")

    current = root
    for offset, component in enumerate(relative_parts):
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise R25PilotContractError("TASK_SOURCE_MISSING", f"{name} does not exist") from exc
        except OSError as exc:
            raise R25PilotContractError("TASK_SOURCE_UNREADABLE", f"cannot inspect {name}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise R25PilotContractError("TASK_SOURCE_SYMLINK", f"{name} crosses a symlink")
        is_last = offset == len(relative_parts) - 1
        if is_last and not stat.S_ISREG(info.st_mode):
            raise R25PilotContractError("INVALID_PATH", f"{name} is not a regular file")
        if not is_last and not stat.S_ISDIR(info.st_mode):
            raise R25PilotContractError("INVALID_PATH", f"{name} has a non-directory parent")

    try:
        root_real = root.resolve(strict=True)
        candidate_real = candidate.resolve(strict=True)
        candidate_real.relative_to(root_real)
    except (OSError, ValueError) as exc:
        raise R25PilotContractError(
            "TASK_SOURCE_OUTSIDE_ROOT", f"{name} resolves outside the authorized input root"
        ) from exc

    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise R25PilotContractError("INVALID_PATH", f"{name} is not a regular file")
        if opened.st_size != expected_byte_count:
            raise R25PilotContractError("TASK_SOURCE_DRIFT", f"{name} byte count changed")
        chunks: list[bytes] = []
        remaining = expected_byte_count + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != expected_byte_count:
            raise R25PilotContractError("TASK_SOURCE_DRIFT", f"{name} byte count changed")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise R25PilotContractError("TASK_SOURCE_DRIFT", f"{name} content hash changed")
        return raw
    except R25PilotContractError:
        raise
    except OSError as exc:
        raise R25PilotContractError("TASK_SOURCE_UNREADABLE", f"cannot read {name}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parse_mobileworld_parameters(
    value: object, *, expected_task_id: str
) -> MobileWorldTaskParametersV1:
    mapping = _require_exact_keys(value, _MOBILEWORLD_PARAMETER_FIELDS, "task parameters")
    parameters = MobileWorldTaskParametersV1(
        task_name=cast(str, mapping["task_name"]),
        trial=cast(int, mapping["trial"]),
    )
    if parameters.task_name != expected_task_id:
        raise R25PilotContractError(
            "TASK_BINDING_MISMATCH", "task parameter task_name does not equal task_id"
        )
    return parameters


def resolve_pilot_task_inputs_v1(
    manifest: FrozenPilotManifestV1,
    *,
    authorized_input_root: str | Path,
    repository_root: str | Path,
) -> ResolvedPilotTaskInputsV1:
    """Resolve exact production task inputs without running MobileWorld.

    The task-source file and every referenced blob must be regular canonical
    JSON files beneath one explicit trust root.  Hash-only v1 CPU fixtures are
    intentionally rejected here.
    """

    if type(manifest) is not FrozenPilotManifestV1:
        raise R25PilotContractError("UNTRUSTED_TYPE", "manifest must use the exact frozen type")
    trusted_manifest = FrozenPilotManifestV1(
        **{field.name: getattr(manifest, field.name) for field in fields(FrozenPilotManifestV1)}
    )

    def trusted_path_text(value: str | Path, name: str) -> str:
        if type(value) is str:
            result = value
        elif isinstance(value, Path):
            result = str(value)
        else:
            raise R25PilotContractError("UNTRUSTED_TYPE", f"{name} type is invalid")
        return _require_absolute_path(result, name)

    root = Path(trusted_path_text(authorized_input_root, "authorized input root"))
    repository = Path(trusted_path_text(repository_root, "repository root"))
    _require_repo_external_input_root(root, repository)

    source_raw = _read_authorized_file(
        trusted_manifest.task_manifest_path,
        authorized_input_root=root,
        expected_sha256=trusted_manifest.task_manifest_sha256,
        expected_byte_count=trusted_manifest.task_manifest_byte_count,
        maximum_byte_count=_MAX_TASK_SOURCE_BYTES,
        name="pilot task source",
    )
    topology_raw = _read_authorized_file(
        trusted_manifest.topology_comparison_artifact_path,
        authorized_input_root=root,
        expected_sha256=trusted_manifest.topology_comparison_artifact_sha256,
        expected_byte_count=trusted_manifest.topology_comparison_artifact_byte_count,
        maximum_byte_count=8 * 1024 * 1024,
        name="CPU topology comparison artifact",
    )
    selection_raw = _read_authorized_file(
        trusted_manifest.cohort_selection_artifact_path,
        authorized_input_root=root,
        expected_sha256=trusted_manifest.cohort_selection_artifact_sha256,
        expected_byte_count=trusted_manifest.cohort_selection_artifact_byte_count,
        maximum_byte_count=8 * 1024 * 1024,
        name="cohort selection artifact",
    )
    topology_value = _decode_canonical_json(topology_raw, "CPU topology comparison artifact")
    try:
        topology_artifact = parse_r24_cpu_topology_artifact(topology_value)
    except ValueError as exc:
        raise R25PilotContractError(
            "INVALID_TOPOLOGY_COMPARISON",
            "CPU topology comparison artifact failed closed validation",
        ) from exc
    if (
        r24_cpu_topology_artifact_sha256(topology_artifact)
        != trusted_manifest.topology_comparison_artifact_sha256
        or topology_artifact.comparison.proposed_pilot_topology.value
        != trusted_manifest.topology.value
        or not topology_artifact.comparison.deployment_topology_frozen
    ):
        raise R25PilotContractError(
            "TOPOLOGY_BINDING_MISMATCH",
            "pilot topology differs from its frozen CPU comparison artifact",
        )
    selection_value = _decode_canonical_json(selection_raw, "cohort selection artifact")
    # Local import avoids making the offline artifact builder an import-time
    # dependency of the pilot value contracts while still requiring its exact
    # parser and current-registry recomputation at production resolution.
    from mobile_world.runtime.sentinel.r2_5.artifact_builder import (
        R25ArtifactBuildError,
        cohort_selection_projection,
        cohort_selection_sha256,
        current_registry_metadata,
        parse_cohort_selection,
        select_gui_only_cohort_from_bytes,
    )

    try:
        selection = parse_cohort_selection(selection_value)
        selection_source_raw = _read_authorized_file(
            selection.source_path,
            authorized_input_root=root,
            expected_sha256=selection.source_sha256,
            expected_byte_count=selection.source_byte_count,
            maximum_byte_count=_MAX_TASK_SOURCE_BYTES,
            name="GUI-only cohort source",
        )
        recomputed_selection = select_gui_only_cohort_from_bytes(
            selection_source_raw,
            Path(selection.source_path),
            current_registry_metadata(),
            cohort_size=len(selection.members),
        )
    except R25PilotContractError:
        raise
    except (R25ArtifactBuildError, OSError, RuntimeError) as exc:
        raise R25PilotContractError(
            "COHORT_SELECTION_RECOMPUTE_FAILED",
            "cohort selection could not be recomputed from current source/registry authority",
        ) from exc
    if (
        cohort_selection_projection(recomputed_selection) != cohort_selection_projection(selection)
        or cohort_selection_sha256(selection) != trusted_manifest.cohort_selection_artifact_sha256
        or trusted_manifest.cohort_selection_sha256
        != trusted_manifest.cohort_selection_artifact_sha256
    ):
        raise R25PilotContractError(
            "COHORT_SELECTION_BINDING_MISMATCH",
            "cohort selection differs from current source/registry recomputation",
        )
    selected_tasks = tuple(
        PilotTaskV1(
            task_id=member.task_id,
            task_parameters_sha256=member.task_parameters_sha256,
            reset_seed=member.reset_seed,
        )
        for member in selection.members
    )
    if selected_tasks != trusted_manifest.tasks:
        raise R25PilotContractError(
            "COHORT_SELECTION_BINDING_MISMATCH",
            "frozen pilot tasks are not the selected source/registry prefix",
        )
    source_value = _decode_canonical_json(source_raw, "pilot task source")
    if type(source_value) is not dict:
        raise R25PilotContractError("UNTRUSTED_TYPE", "pilot task source must be an object")
    if cast(dict[object, object], source_value).get("schema_version") != (
        EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION
    ):
        raise R25PilotContractError(
            "UNEXECUTABLE_TASK_SOURCE", "hash-only or unknown task source cannot be executed"
        )
    source = _require_exact_keys(source_value, _EXECUTABLE_SOURCE_FIELDS, "pilot task source")
    if source["cohort_id"] != trusted_manifest.cohort_id:
        raise R25PilotContractError("TASK_BINDING_MISMATCH", "cohort IDs do not match")
    if source["seed_policy"] != trusted_manifest.seed_policy.value:
        raise R25PilotContractError("TASK_BINDING_MISMATCH", "seed policies do not match")
    raw_tasks = source["tasks"]
    if type(raw_tasks) is not list or len(raw_tasks) != len(trusted_manifest.tasks):
        raise R25PilotContractError(
            "TASK_BINDING_MISMATCH", "task source does not match the frozen cohort size"
        )

    resolved: list[PilotResetTaskInitInputV1] = []
    for frozen_task, raw_task in zip(
        trusted_manifest.tasks, cast(list[object], raw_tasks), strict=True
    ):
        task = _require_exact_keys(raw_task, _EXECUTABLE_TASK_FIELDS, "executable task")
        source_task = PilotTaskV1(
            task_id=cast(str, task["task_id"]),
            task_parameters_sha256=cast(str, task["task_parameters_sha256"]),
            reset_seed=cast(int, task["reset_seed"]),
        )
        if source_task != frozen_task:
            raise R25PilotContractError(
                "TASK_BINDING_MISMATCH", "executable task differs from the frozen manifest"
            )
        parameter_source_value = task["parameter_source"]
        if type(parameter_source_value) is not dict:
            raise R25PilotContractError("UNTRUSTED_TYPE", "parameter source must be an object")
        parameter_source = cast(dict[object, object], parameter_source_value)
        raw_kind = parameter_source.get("kind")
        try:
            kind = PilotTaskParameterSourceKindV1(cast(str, raw_kind))
        except (TypeError, ValueError) as exc:
            raise R25PilotContractError("INVALID_ENUM", "parameter source kind is unknown") from exc

        if kind is PilotTaskParameterSourceKindV1.INLINE_CANONICAL_JSON:
            inline = _require_exact_keys(
                parameter_source_value, _INLINE_SOURCE_FIELDS, "inline parameter source"
            )
            payload = _require_bounded_json_tree(inline["payload"], "inline task parameters")
            parameter_raw = canonical_json_bytes(payload)
            declared_sha256 = _require_sha256(inline["sha256"], "inline parameter sha256")
            declared_bytes = _require_positive_int(
                inline["byte_count"], "inline parameter byte_count", _MAX_TASK_PARAMETERS_BYTES
            )
            if (
                len(parameter_raw) != declared_bytes
                or hashlib.sha256(parameter_raw).hexdigest() != declared_sha256
            ):
                raise R25PilotContractError(
                    "TASK_SOURCE_DRIFT", "inline parameter digest/size is inconsistent"
                )
            parameter_value = payload
        else:
            external = _require_exact_keys(
                parameter_source_value, _EXTERNAL_SOURCE_FIELDS, "external parameter source"
            )
            declared_sha256 = _require_sha256(external["sha256"], "parameter blob sha256")
            declared_bytes = _require_positive_int(
                external["byte_count"], "parameter blob byte_count", _MAX_TASK_PARAMETERS_BYTES
            )
            parameter_raw = _read_authorized_file(
                cast(str, external["path"]),
                authorized_input_root=root,
                expected_sha256=declared_sha256,
                expected_byte_count=declared_bytes,
                maximum_byte_count=_MAX_TASK_PARAMETERS_BYTES,
                name=f"parameter blob for {frozen_task.task_id}",
            )
            parameter_value = _decode_canonical_json(
                parameter_raw, f"parameter blob for {frozen_task.task_id}"
            )

        if declared_sha256 != frozen_task.task_parameters_sha256:
            raise R25PilotContractError(
                "TASK_BINDING_MISMATCH", "parameter digest differs from the frozen task"
            )
        parameters = _parse_mobileworld_parameters(
            parameter_value, expected_task_id=frozen_task.task_id
        )
        resolved.append(
            PilotResetTaskInitInputV1(
                task_id=frozen_task.task_id,
                task_name=parameters.task_name,
                trial=parameters.trial,
                task_parameters_sha256=frozen_task.task_parameters_sha256,
                reset_seed=frozen_task.reset_seed,
                seed_policy=trusted_manifest.seed_policy,
                parameter_source_kind=kind,
                task_time_authority=trusted_manifest.task_time_authority,
                cohort_selection_sha256=trusted_manifest.cohort_selection_sha256,
            )
        )

    return ResolvedPilotTaskInputsV1(
        schema_version=RESOLVED_PILOT_TASK_INPUTS_SCHEMA_VERSION,
        cohort_id=trusted_manifest.cohort_id,
        frozen_pilot_manifest_sha256=frozen_pilot_manifest_sha256(trusted_manifest),
        task_source_sha256=trusted_manifest.task_manifest_sha256,
        task_source_byte_count=trusted_manifest.task_manifest_byte_count,
        seed_policy=trusted_manifest.seed_policy,
        task_time_authority=trusted_manifest.task_time_authority,
        cohort_selection_sha256=trusted_manifest.cohort_selection_sha256,
        tasks=tuple(resolved),
        _seal=_RESOLUTION_SEAL,
    )


__all__ = [
    "EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION",
    "FROZEN_PILOT_SCHEMA_VERSION",
    "PILOT_TASK_SOURCE_SCHEMA_VERSION",
    "RESOLVED_PILOT_TASK_INPUTS_SCHEMA_VERSION",
    "ExternalPilotTaskParametersV1",
    "FrozenPilotManifestV1",
    "InlinePilotTaskParametersV1",
    "MobileWorldTaskParametersV1",
    "PilotArmV1",
    "PilotCellV1",
    "PilotHostV1",
    "PilotResetTaskInitInputV1",
    "PilotSeedPolicyV1",
    "PilotTaskParameterBindingV1",
    "PilotTaskParameterSourceKindV1",
    "PilotTaskTimeAuthorityV1",
    "PilotTaskV1",
    "PilotTopologyV1",
    "R25PilotContractError",
    "ResolvedPilotTaskInputsV1",
    "executable_pilot_task_source_projection",
    "frozen_pilot_manifest_projection",
    "frozen_pilot_manifest_sha256",
    "parse_frozen_pilot_manifest",
    "pilot_task_source_projection",
    "resolve_pilot_task_inputs_v1",
    "resolved_pilot_task_inputs_projection",
    "resolved_pilot_task_inputs_sha256",
]
