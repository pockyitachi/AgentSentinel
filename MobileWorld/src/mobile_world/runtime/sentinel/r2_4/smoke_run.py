"""Closed R2.4-only live-smoke authority contract.

This module is deliberately additive to the joint R2.4/R2.5 v1 contract.  An
R2.4 smoke authority contains no pilot, cohort, task-source, or topology
artifact field and therefore cannot be interpreted as R2.5 authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_run import (
    HostLiveSmokePlanV1,
    LiveRunContractError,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SnapshotResourceV1,
    _enum,
    _exact_object,
    _openai_projection,
    _parse_openai,
    _parse_resource,
    _parse_secret,
    _parse_smoke_plan,
    _require_bool,
    _require_id,
    _require_int,
    _require_path,
    _require_sha256,
    _require_timestamp,
    _resource_projection,
    _secret_projection,
    _smoke_plan_projection,
    _timestamp,
)
from mobile_world.runtime.sentinel.r2_5.pilot import PilotHostV1

R24_SMOKE_AUTHORITY_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-live-smoke-run-authority/v1"
R24_SMOKE_SEQUENCE_RESULT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-smoke-sequence-result/v1"
)
R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS = 8
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}")


class SequenceExecutionScopeV1(StrEnum):
    """Mutually exclusive sequence scopes used at production boundaries."""

    R24_R25_FULL = "R24_R25_FULL"
    R24_LIVE_SMOKE_ONLY = "R24_LIVE_SMOKE_ONLY"


@dataclass(frozen=True, slots=True)
class R24SmokeOwnerAuthorizationV1:
    """Owner authority that explicitly withholds every GUI action and R2.5."""

    status: RunAuthorizationStatusV1
    authorization_id: str
    authorized_by: str
    issued_at_utc: str
    expires_at_utc: str
    network_allowed: bool
    gpu_allowed: bool
    docker_allowed: bool
    model_loading_allowed: bool
    backend_allowed: bool
    actor_model_calls_allowed: bool
    sentinel_provider_calls_allowed: bool
    smoke_gui_actions_allowed: bool
    merge_allowed: bool
    linear_update_allowed: bool
    frozen_artifact_mutation_allowed: bool

    def __post_init__(self) -> None:
        if type(self.status) is not RunAuthorizationStatusV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "authorization status is untrusted")
        _require_id(self.authorization_id, "authorization_id")
        _require_id(self.authorized_by, "authorized_by")
        issued = _timestamp(_require_timestamp(self.issued_at_utc, "issued_at_utc"))
        expires = _timestamp(_require_timestamp(self.expires_at_utc, "expires_at_utc"))
        if expires <= issued:
            raise LiveRunContractError("INVALID_AUTHORITY_WINDOW", "authority expiry is not later")
        for name in (
            "network_allowed",
            "gpu_allowed",
            "docker_allowed",
            "model_loading_allowed",
            "backend_allowed",
            "actor_model_calls_allowed",
            "sentinel_provider_calls_allowed",
        ):
            _require_bool(getattr(self, name), name, True)
        for name in (
            "smoke_gui_actions_allowed",
            "merge_allowed",
            "linear_update_allowed",
            "frozen_artifact_mutation_allowed",
        ):
            _require_bool(getattr(self, name), name, False)


@dataclass(frozen=True, slots=True)
class R24SmokeSequenceSafetyV1:
    stages: tuple[RunStageV1, ...]
    stop_on_failure: bool
    pilot_stage_forbidden: bool
    default_dry_run: bool
    arbitrary_commands_forbidden: bool
    secrets_in_logs_forbidden: bool
    repo_external_output_required: bool

    def __post_init__(self) -> None:
        if self.stages != (
            RunStageV1.RESOURCE_PREFLIGHT,
            RunStageV1.QWEN_LIVE_SMOKE,
            RunStageV1.MAI_LIVE_SMOKE,
        ):
            raise LiveRunContractError("INVALID_STAGE_ORDER", "R2.4 smoke stage order is not exact")
        for name in (
            "stop_on_failure",
            "pilot_stage_forbidden",
            "default_dry_run",
            "arbitrary_commands_forbidden",
            "secrets_in_logs_forbidden",
            "repo_external_output_required",
        ):
            _require_bool(getattr(self, name), name, True)


@dataclass(frozen=True, slots=True)
class R24SmokeRunAuthorityManifestV1:
    """Exact authority for resource preparation and six R2.4 smoke cases only."""

    schema_version: str
    execution_scope: SequenceExecutionScopeV1
    run_id: str
    source_commit: str
    authorization: R24SmokeOwnerAuthorizationV1
    safety: R24SmokeSequenceSafetyV1
    secret: SecretFileReferenceV1
    openai_stages: tuple[OpenAIResponsesStageV1, ...]
    actor_resources: tuple[SnapshotResourceV1, ...]
    smoke_plans: tuple[HostLiveSmokePlanV1, ...]
    resource_topology: str
    runtime_config_sha256: str
    output_root: str
    max_resource_preflight_wall_time_seconds: int
    max_qwen_to_mai_handoff_wall_time_seconds: int
    max_resource_cleanup_wall_time_seconds: int
    max_sequence_wall_time_seconds: int
    max_sequence_openai_calls: int
    max_sequence_actor_calls: int
    max_sequence_cost_usd_micros: int

    def __post_init__(self) -> None:
        if self.schema_version != R24_SMOKE_AUTHORITY_SCHEMA_VERSION:
            raise LiveRunContractError("UNKNOWN_SCHEMA", "unknown R2.4 smoke authority schema")
        if self.execution_scope is not SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY:
            raise LiveRunContractError("INVALID_EXECUTION_SCOPE", "authority is not smoke-only")
        _require_id(self.run_id, "run_id")
        if type(self.source_commit) is not str or _GIT_SHA1.fullmatch(self.source_commit) is None:
            raise LiveRunContractError("INVALID_COMMIT", "source_commit must be full SHA-1")
        if type(self.authorization) is not R24SmokeOwnerAuthorizationV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "smoke authorization is untrusted")
        if type(self.safety) is not R24SmokeSequenceSafetyV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "smoke safety is untrusted")
        if type(self.secret) is not SecretFileReferenceV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "secret reference is untrusted")
        if type(self.openai_stages) is not tuple or tuple(
            stage.role for stage in self.openai_stages
        ) != (OpenAIRoleV1.RUBRIC, OpenAIRoleV1.HISTORY_POLICY):
            raise LiveRunContractError("INVALID_OPENAI_MATRIX", "OpenAI stages differ")
        if any(type(stage) is not OpenAIResponsesStageV1 for stage in self.openai_stages):
            raise LiveRunContractError("UNTRUSTED_TYPE", "OpenAI stage is untrusted")
        if type(self.actor_resources) is not tuple or tuple(
            resource.host for resource in self.actor_resources
        ) != (PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI):
            raise LiveRunContractError("INVALID_HOST_MATRIX", "resources must be Qwen then MAI")
        if any(type(item) is not SnapshotResourceV1 for item in self.actor_resources):
            raise LiveRunContractError("UNTRUSTED_TYPE", "actor resource is untrusted")
        if type(self.smoke_plans) is not tuple or tuple(plan.host for plan in self.smoke_plans) != (
            PilotHostV1.QWEN3_VL,
            PilotHostV1.MAI_UI,
        ):
            raise LiveRunContractError("INVALID_HOST_MATRIX", "smokes must be Qwen then MAI")
        if any(type(item) is not HostLiveSmokePlanV1 for item in self.smoke_plans):
            raise LiveRunContractError("UNTRUSTED_TYPE", "smoke plan is untrusted")
        if self.resource_topology != "SINGLE_GPU_SEQUENTIAL_SHARED":
            raise LiveRunContractError(
                "INVALID_RESOURCE_TOPOLOGY", "R2.4 smoke authority requires shared single GPU"
            )
        _require_sha256(self.runtime_config_sha256, "runtime_config_sha256")
        _require_path(self.output_root, "output_root")
        _require_int(
            self.max_resource_preflight_wall_time_seconds,
            "max_resource_preflight_wall_time_seconds",
            1,
            86_400,
        )
        _require_int(
            self.max_qwen_to_mai_handoff_wall_time_seconds,
            "max_qwen_to_mai_handoff_wall_time_seconds",
            1,
            3_600,
        )
        _require_int(
            self.max_resource_cleanup_wall_time_seconds,
            "max_resource_cleanup_wall_time_seconds",
            R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS,
            3_600,
        )
        _require_int(
            self.max_sequence_wall_time_seconds, "max_sequence_wall_time_seconds", 1, 86_400
        )
        smoke_cases = tuple(case for plan in self.smoke_plans for case in plan.cases)
        expected_actor = sum(case.max_actor_calls for case in smoke_cases)
        expected_openai = sum(case.max_openai_calls for case in smoke_cases)
        expected_cost = sum(case.max_cost_usd_micros for case in smoke_cases)
        expected_time = (
            self.max_resource_preflight_wall_time_seconds
            + self.max_qwen_to_mai_handoff_wall_time_seconds
            + self.max_resource_cleanup_wall_time_seconds
            + sum(case.max_wall_time_seconds for case in smoke_cases)
        )
        if (
            self.max_sequence_actor_calls != expected_actor
            or self.max_sequence_openai_calls != expected_openai
            or self.max_sequence_cost_usd_micros != expected_cost
            or self.max_sequence_wall_time_seconds != expected_time
        ):
            raise LiveRunContractError(
                "BUDGET_BINDING_MISMATCH", "smoke-only sequence budget is not additive"
            )


def _authorization_projection(value: R24SmokeOwnerAuthorizationV1) -> dict[str, JsonValue]:
    if type(value) is not R24SmokeOwnerAuthorizationV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "smoke authorization is untrusted")
    trusted = R24SmokeOwnerAuthorizationV1(
        **{field.name: getattr(value, field.name) for field in fields(value)}
    )
    return {
        "actor_model_calls_allowed": trusted.actor_model_calls_allowed,
        "authorization_id": trusted.authorization_id,
        "authorized_by": trusted.authorized_by,
        "backend_allowed": trusted.backend_allowed,
        "docker_allowed": trusted.docker_allowed,
        "expires_at_utc": trusted.expires_at_utc,
        "frozen_artifact_mutation_allowed": trusted.frozen_artifact_mutation_allowed,
        "gpu_allowed": trusted.gpu_allowed,
        "issued_at_utc": trusted.issued_at_utc,
        "linear_update_allowed": trusted.linear_update_allowed,
        "merge_allowed": trusted.merge_allowed,
        "model_loading_allowed": trusted.model_loading_allowed,
        "network_allowed": trusted.network_allowed,
        "sentinel_provider_calls_allowed": trusted.sentinel_provider_calls_allowed,
        "smoke_gui_actions_allowed": trusted.smoke_gui_actions_allowed,
        "status": trusted.status.value,
    }


def _safety_projection(value: R24SmokeSequenceSafetyV1) -> dict[str, JsonValue]:
    if type(value) is not R24SmokeSequenceSafetyV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "smoke safety is untrusted")
    return {
        "arbitrary_commands_forbidden": value.arbitrary_commands_forbidden,
        "default_dry_run": value.default_dry_run,
        "pilot_stage_forbidden": value.pilot_stage_forbidden,
        "repo_external_output_required": value.repo_external_output_required,
        "secrets_in_logs_forbidden": value.secrets_in_logs_forbidden,
        "stages": [stage.value for stage in value.stages],
        "stop_on_failure": value.stop_on_failure,
    }


def smoke_authority_manifest_projection(
    value: R24SmokeRunAuthorityManifestV1,
) -> dict[str, JsonValue]:
    if type(value) is not R24SmokeRunAuthorityManifestV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "smoke authority is untrusted")
    trusted = R24SmokeRunAuthorityManifestV1(
        **{field.name: getattr(value, field.name) for field in fields(value)}
    )
    return {
        "actor_resources": [
            cast(JsonValue, _resource_projection(item)) for item in trusted.actor_resources
        ],
        "authorization": cast(JsonValue, _authorization_projection(trusted.authorization)),
        "execution_scope": trusted.execution_scope.value,
        "max_resource_preflight_wall_time_seconds": (
            trusted.max_resource_preflight_wall_time_seconds
        ),
        "max_resource_cleanup_wall_time_seconds": (trusted.max_resource_cleanup_wall_time_seconds),
        "max_qwen_to_mai_handoff_wall_time_seconds": (
            trusted.max_qwen_to_mai_handoff_wall_time_seconds
        ),
        "max_sequence_actor_calls": trusted.max_sequence_actor_calls,
        "max_sequence_cost_usd_micros": trusted.max_sequence_cost_usd_micros,
        "max_sequence_openai_calls": trusted.max_sequence_openai_calls,
        "max_sequence_wall_time_seconds": trusted.max_sequence_wall_time_seconds,
        "openai_stages": [
            cast(JsonValue, _openai_projection(item)) for item in trusted.openai_stages
        ],
        "output_root": trusted.output_root,
        "resource_topology": trusted.resource_topology,
        "run_id": trusted.run_id,
        "safety": cast(JsonValue, _safety_projection(trusted.safety)),
        "schema_version": trusted.schema_version,
        "secret": cast(JsonValue, _secret_projection(trusted.secret)),
        "smoke_plans": [
            cast(JsonValue, _smoke_plan_projection(item)) for item in trusted.smoke_plans
        ],
        "source_commit": trusted.source_commit,
        "runtime_config_sha256": trusted.runtime_config_sha256,
    }


def smoke_authority_manifest_sha256(value: R24SmokeRunAuthorityManifestV1) -> str:
    return canonical_sha256(cast(JsonValue, smoke_authority_manifest_projection(value)))


_AUTHORIZATION_FIELDS = frozenset(field.name for field in fields(R24SmokeOwnerAuthorizationV1))
_SAFETY_FIELDS = frozenset(field.name for field in fields(R24SmokeSequenceSafetyV1))
_MANIFEST_FIELDS = frozenset(field.name for field in fields(R24SmokeRunAuthorityManifestV1))


def _parse_authorization(value: object) -> R24SmokeOwnerAuthorizationV1:
    item = _exact_object(value, _AUTHORIZATION_FIELDS, "smoke authorization")
    return R24SmokeOwnerAuthorizationV1(
        status=cast(
            RunAuthorizationStatusV1,
            _enum(RunAuthorizationStatusV1, item["status"], "status"),
        ),
        authorization_id=cast(str, item["authorization_id"]),
        authorized_by=cast(str, item["authorized_by"]),
        issued_at_utc=cast(str, item["issued_at_utc"]),
        expires_at_utc=cast(str, item["expires_at_utc"]),
        network_allowed=cast(bool, item["network_allowed"]),
        gpu_allowed=cast(bool, item["gpu_allowed"]),
        docker_allowed=cast(bool, item["docker_allowed"]),
        model_loading_allowed=cast(bool, item["model_loading_allowed"]),
        backend_allowed=cast(bool, item["backend_allowed"]),
        actor_model_calls_allowed=cast(bool, item["actor_model_calls_allowed"]),
        sentinel_provider_calls_allowed=cast(bool, item["sentinel_provider_calls_allowed"]),
        smoke_gui_actions_allowed=cast(bool, item["smoke_gui_actions_allowed"]),
        merge_allowed=cast(bool, item["merge_allowed"]),
        linear_update_allowed=cast(bool, item["linear_update_allowed"]),
        frozen_artifact_mutation_allowed=cast(bool, item["frozen_artifact_mutation_allowed"]),
    )


def _parse_safety(value: object) -> R24SmokeSequenceSafetyV1:
    item = _exact_object(value, _SAFETY_FIELDS, "smoke safety")
    raw_stages = item["stages"]
    if type(raw_stages) is not list:
        raise LiveRunContractError("UNTRUSTED_TYPE", "smoke stages must be an array")
    return R24SmokeSequenceSafetyV1(
        stages=tuple(
            cast(RunStageV1, _enum(RunStageV1, stage, "run stage"))
            for stage in cast(list[object], raw_stages)
        ),
        stop_on_failure=cast(bool, item["stop_on_failure"]),
        pilot_stage_forbidden=cast(bool, item["pilot_stage_forbidden"]),
        default_dry_run=cast(bool, item["default_dry_run"]),
        arbitrary_commands_forbidden=cast(bool, item["arbitrary_commands_forbidden"]),
        secrets_in_logs_forbidden=cast(bool, item["secrets_in_logs_forbidden"]),
        repo_external_output_required=cast(bool, item["repo_external_output_required"]),
    )


def parse_smoke_authority_manifest(value: object) -> R24SmokeRunAuthorityManifestV1:
    item = _exact_object(value, _MANIFEST_FIELDS, "R2.4 smoke authority")
    raw_openai = item["openai_stages"]
    raw_resources = item["actor_resources"]
    raw_smokes = item["smoke_plans"]
    if any(type(value) is not list for value in (raw_openai, raw_resources, raw_smokes)):
        raise LiveRunContractError("UNTRUSTED_TYPE", "manifest collections must be arrays")
    return R24SmokeRunAuthorityManifestV1(
        schema_version=cast(str, item["schema_version"]),
        execution_scope=cast(
            SequenceExecutionScopeV1,
            _enum(SequenceExecutionScopeV1, item["execution_scope"], "execution scope"),
        ),
        run_id=cast(str, item["run_id"]),
        source_commit=cast(str, item["source_commit"]),
        authorization=_parse_authorization(item["authorization"]),
        safety=_parse_safety(item["safety"]),
        secret=_parse_secret(item["secret"]),
        openai_stages=tuple(_parse_openai(item) for item in cast(list[object], raw_openai)),
        actor_resources=tuple(_parse_resource(item) for item in cast(list[object], raw_resources)),
        smoke_plans=tuple(_parse_smoke_plan(item) for item in cast(list[object], raw_smokes)),
        resource_topology=cast(str, item["resource_topology"]),
        runtime_config_sha256=cast(str, item["runtime_config_sha256"]),
        output_root=cast(str, item["output_root"]),
        max_resource_preflight_wall_time_seconds=cast(
            int, item["max_resource_preflight_wall_time_seconds"]
        ),
        max_resource_cleanup_wall_time_seconds=cast(
            int, item["max_resource_cleanup_wall_time_seconds"]
        ),
        max_qwen_to_mai_handoff_wall_time_seconds=cast(
            int, item["max_qwen_to_mai_handoff_wall_time_seconds"]
        ),
        max_sequence_wall_time_seconds=cast(int, item["max_sequence_wall_time_seconds"]),
        max_sequence_openai_calls=cast(int, item["max_sequence_openai_calls"]),
        max_sequence_actor_calls=cast(int, item["max_sequence_actor_calls"]),
        max_sequence_cost_usd_micros=cast(int, item["max_sequence_cost_usd_micros"]),
    )


__all__ = [
    "R24_SMOKE_AUTHORITY_SCHEMA_VERSION",
    "R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS",
    "R24_SMOKE_SEQUENCE_RESULT_SCHEMA_VERSION",
    "R24SmokeOwnerAuthorizationV1",
    "R24SmokeRunAuthorityManifestV1",
    "R24SmokeSequenceSafetyV1",
    "SequenceExecutionScopeV1",
    "parse_smoke_authority_manifest",
    "smoke_authority_manifest_projection",
    "smoke_authority_manifest_sha256",
]
