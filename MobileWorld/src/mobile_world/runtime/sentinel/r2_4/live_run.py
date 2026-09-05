"""Fail-closed CPU planning for one R2.4-live-then-R2.5 run authority.

Nothing in this module performs a provider call, probes a GPU, starts Docker,
loads a model, starts a backend, or executes an action.  The command-line
wrapper is dry-run-only until a module-owned production executor is added.

The owner manifest is intentionally data, not an executable command list.  A
manifest can therefore authorize a later implementation without becoming a
shell-injection surface.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlsplit

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_sha256
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FrozenPilotManifestV1,
    PilotHostV1,
    R25PilotContractError,
    frozen_pilot_manifest_projection,
    frozen_pilot_manifest_sha256,
    parse_frozen_pilot_manifest,
    resolve_pilot_task_inputs_v1,
)

R24_R25_RUN_AUTHORITY_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-r2.5-run-authority/v1"
R24_R25_PREFLIGHT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-r2.5-preflight/v1"
R24_R25_SEQUENCE_RESULT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-r2.5-sequence-result/v1"
SNAPSHOT_TREE_ALGORITHM_V1 = "SHA256_LOGICAL_TREE_V1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_UTC_SECOND = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_MAX_MANIFEST_BYTES = 1_048_576
_QWEN_CODEC = "mobileworld.g1.history-codec.qwen-flat-progress"
_MAI_CODEC = "mobileworld.g1.history-codec.mai-raw-replay"


class LiveRunContractError(ValueError):
    """Stable error that never includes secret content or environment values."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class RunAuthorizationStatusV1(StrEnum):
    DRAFT_NOT_AUTHORIZED = "DRAFT_NOT_AUTHORIZED"
    OWNER_AUTHORIZED = "OWNER_AUTHORIZED"


class RunStageV1(StrEnum):
    RESOURCE_PREFLIGHT = "RESOURCE_PREFLIGHT"
    QWEN_LIVE_SMOKE = "QWEN_LIVE_SMOKE"
    MAI_LIVE_SMOKE = "MAI_LIVE_SMOKE"
    R25_PILOT = "R25_PILOT"


class SmokeModeV1(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


class OpenAIRoleV1(StrEnum):
    RUBRIC = "RUBRIC"
    HISTORY_POLICY = "HISTORY_POLICY"


class SequenceStatusV1(StrEnum):
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


def _require_id(value: object, name: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise LiveRunContractError("INVALID_ID", f"{name} is invalid")
    return value


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise LiveRunContractError("INVALID_SHA256", f"{name} is invalid")
    return value


def _require_bool(value: object, name: str, required: bool) -> bool:
    if type(value) is not bool or value is not required:
        raise LiveRunContractError("INVALID_SAFETY_FLAG", f"{name} must be {required}")
    return value


def _require_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise LiveRunContractError("INVALID_BOUND", f"{name} is outside its closed bound")
    return value


def _require_timestamp(value: object, name: str) -> str:
    if type(value) is not str or _UTC_SECOND.fullmatch(value) is None:
        raise LiveRunContractError("INVALID_TIMESTAMP", f"{name} must be UTC to seconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise LiveRunContractError("INVALID_TIMESTAMP", f"{name} is not a real date") from exc
    return value


def _timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _require_path(value: object, name: str) -> str:
    if type(value) is not str or not value or "\x00" in value or len(value) > 4096:
        raise LiveRunContractError("INVALID_PATH", f"{name} is invalid")
    if not Path(value).is_absolute():
        raise LiveRunContractError("INVALID_PATH", f"{name} must be absolute")
    return value


def _exact_object(value: object, expected: frozenset[str], name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise LiveRunContractError("UNTRUSTED_TYPE", f"{name} must be an object")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != expected:
        raise LiveRunContractError("INVALID_FIELDS", f"{name} fields do not match the contract")
    return cast(dict[str, object], mapping)


def _enum(enum_type: type[StrEnum], value: object, name: str) -> StrEnum:
    if type(value) is not str:
        raise LiveRunContractError("INVALID_ENUM", f"{name} is invalid")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise LiveRunContractError("INVALID_ENUM", f"{name} is unknown") from exc


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class OwnerAuthorizationV1:
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
    pilot_gui_actions_allowed: bool
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
            "pilot_gui_actions_allowed",
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
class SecretFileReferenceV1:
    path: str
    environment_key: str
    required_mode: int
    content_may_be_read_by_preflight: bool
    persist_value_or_hash: bool

    def __post_init__(self) -> None:
        _require_path(self.path, "secret.path")
        if self.environment_key != "OPENAI_API_KEY":
            raise LiveRunContractError(
                "INVALID_SECRET_REFERENCE", "only OPENAI_API_KEY is supported"
            )
        if type(self.required_mode) is not int or self.required_mode != 0o600:
            raise LiveRunContractError("INVALID_SECRET_MODE", "secret mode must be 0600")
        _require_bool(
            self.content_may_be_read_by_preflight, "content_may_be_read_by_preflight", False
        )
        _require_bool(self.persist_value_or_hash, "persist_value_or_hash", False)


@dataclass(frozen=True, slots=True)
class OpenAIResponsesStageV1:
    role: OpenAIRoleV1
    model: str
    endpoint: str
    transport_kind: str
    transport_authority: str
    openai_sdk_version: str
    sdk_max_retries: int
    external_network_on_call: bool
    model_on_call: bool
    max_output_tokens: int
    timeout_ms: int
    max_attempts: int
    store: bool

    def __post_init__(self) -> None:
        if type(self.role) is not OpenAIRoleV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "OpenAI role is untrusted")
        if type(self.model) is not str or _MODEL_ID.fullmatch(self.model) is None:
            raise LiveRunContractError("INVALID_MODEL", "OpenAI model ID is invalid")
        if self.model != "gpt-5.6-sol":
            raise LiveRunContractError("INVALID_MODEL", "OpenAI stage model differs")
        if type(self.endpoint) is not str:
            raise LiveRunContractError("INVALID_OPENAI_ENDPOINT", "Responses endpoint is not text")
        try:
            parsed = urlsplit(self.endpoint)
            parsed_port = parsed.port
        except ValueError as exc:
            raise LiveRunContractError(
                "INVALID_OPENAI_ENDPOINT", "Responses endpoint is malformed"
            ) from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.openai.com"
            or parsed_port not in {None, 443}
            or parsed.path != "/v1/responses"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise LiveRunContractError("INVALID_OPENAI_ENDPOINT", "Responses endpoint is not exact")
        if (
            self.transport_kind != "OPENAI_RESPONSES"
            or self.transport_authority != "EXPLICIT_OWNER_AUTHORIZATION"
            or self.openai_sdk_version != "1.106.1"
            or type(self.sdk_max_retries) is not int
            or self.sdk_max_retries != 0
            or self.external_network_on_call is not True
            or self.model_on_call is not True
        ):
            raise LiveRunContractError(
                "INVALID_TRANSPORT_DESCRIPTOR", "OpenAI transport declaration differs"
            )
        _require_int(self.max_output_tokens, "max_output_tokens", 1, 16_384)
        if self.role is OpenAIRoleV1.HISTORY_POLICY and self.max_output_tokens != 4096:
            raise LiveRunContractError(
                "INVALID_OPENAI_CONFIG", "history policy output bound differs from R2.2"
            )
        if self.role is OpenAIRoleV1.RUBRIC and self.max_output_tokens != 8192:
            raise LiveRunContractError(
                "INVALID_OPENAI_CONFIG", "rubric output bound differs from R2.4"
            )
        _require_int(self.timeout_ms, "timeout_ms", 1, 300_000)
        if type(self.max_attempts) is not int or self.max_attempts != 1:
            raise LiveRunContractError("RETRIES_FORBIDDEN", "live stages allow one attempt")
        _require_bool(self.store, "store", False)


@dataclass(frozen=True, slots=True)
class SnapshotResourceV1:
    host: PilotHostV1
    history_codec_id: str
    snapshot_path: str
    snapshot_storage_root: str
    snapshot_tree_algorithm: str
    snapshot_tree_sha256: str
    snapshot_total_bytes: int
    snapshot_file_count: int
    actor_endpoint: str
    served_model_id: str
    host_enabled: bool
    independent_kill_switch: bool

    def __post_init__(self) -> None:
        if type(self.host) is not PilotHostV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "snapshot host is untrusted")
        expected_codec = _QWEN_CODEC if self.host is PilotHostV1.QWEN3_VL else _MAI_CODEC
        if self.history_codec_id != expected_codec:
            raise LiveRunContractError("CODEC_HOST_MISMATCH", "snapshot has the wrong codec")
        _require_path(self.snapshot_path, "snapshot_path")
        _require_path(self.snapshot_storage_root, "snapshot_storage_root")
        if self.snapshot_tree_algorithm != SNAPSHOT_TREE_ALGORITHM_V1:
            raise LiveRunContractError("INVALID_TREE_ALGORITHM", "snapshot tree algorithm differs")
        _require_sha256(self.snapshot_tree_sha256, "snapshot_tree_sha256")
        _require_int(self.snapshot_total_bytes, "snapshot_total_bytes", 1, 10_000_000_000_000)
        _require_int(self.snapshot_file_count, "snapshot_file_count", 1, 1_000_000)
        if type(self.actor_endpoint) is not str:
            raise LiveRunContractError("ACTOR_ENDPOINT_NOT_LOOPBACK", "actor endpoint is not text")
        try:
            parsed = urlsplit(self.actor_endpoint)
            parsed_port = parsed.port
        except ValueError as exc:
            raise LiveRunContractError(
                "ACTOR_ENDPOINT_NOT_LOOPBACK", "actor endpoint is malformed"
            ) from exc
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as exc:
            raise LiveRunContractError(
                "ACTOR_ENDPOINT_NOT_LOOPBACK", "actor endpoint is not IP loopback"
            ) from exc
        if (
            parsed.scheme != "http"
            or not address.is_loopback
            or parsed_port is None
            or not 1024 <= parsed_port <= 65535
            or parsed.path not in {"", "/v1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise LiveRunContractError(
                "ACTOR_ENDPOINT_NOT_LOOPBACK", "actor endpoint is not exact loopback"
            )
        if (
            type(self.served_model_id) is not str
            or _MODEL_ID.fullmatch(self.served_model_id) is None
        ):
            raise LiveRunContractError("INVALID_MODEL", "served model ID is invalid")
        _require_bool(self.host_enabled, "host_enabled", True)
        _require_bool(self.independent_kill_switch, "independent_kill_switch", True)


@dataclass(frozen=True, slots=True)
class LiveSmokeCaseV1:
    case_id: str
    task_id: str
    mode: SmokeModeV1
    request_fixture_path: str
    request_fixture_sha256: str
    request_fixture_byte_count: int
    max_actor_calls: int
    max_openai_calls: int
    max_wall_time_seconds: int
    max_cost_usd_micros: int
    actor_action_allowed: bool
    provider_final_request_proof_required: bool

    def __post_init__(self) -> None:
        _require_id(self.case_id, "case_id")
        _require_id(self.task_id, "task_id")
        if type(self.mode) is not SmokeModeV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "smoke mode is untrusted")
        _require_path(self.request_fixture_path, "request_fixture_path")
        _require_sha256(self.request_fixture_sha256, "request_fixture_sha256")
        _require_int(self.request_fixture_byte_count, "request_fixture_byte_count", 1, 100_000_000)
        if type(self.max_actor_calls) is not int or self.max_actor_calls != 1:
            raise LiveRunContractError("INVALID_SMOKE_BOUND", "each smoke case has one actor call")
        # One independent rubric generation, one history-free rubric tracking
        # call, and one history-policy call.  OFF performs no semantic work.
        expected_openai_max = 0 if self.mode is SmokeModeV1.OFF else 3
        _require_int(self.max_openai_calls, "max_openai_calls", 0, expected_openai_max)
        if self.mode is not SmokeModeV1.OFF and self.max_openai_calls != 3:
            raise LiveRunContractError(
                "INVALID_SMOKE_BOUND",
                "semantic smoke needs generate, track, and history-policy budgets",
            )
        _require_int(self.max_wall_time_seconds, "max_wall_time_seconds", 1, 1_800)
        _require_int(self.max_cost_usd_micros, "max_cost_usd_micros", 1, 1_000_000_000)
        _require_bool(self.actor_action_allowed, "actor_action_allowed", False)
        _require_bool(
            self.provider_final_request_proof_required,
            "provider_final_request_proof_required",
            True,
        )


@dataclass(frozen=True, slots=True)
class HostLiveSmokePlanV1:
    host: PilotHostV1
    cases: tuple[LiveSmokeCaseV1, ...]

    def __post_init__(self) -> None:
        if type(self.host) is not PilotHostV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "smoke host is untrusted")
        if type(self.cases) is not tuple or any(
            type(case) is not LiveSmokeCaseV1 for case in self.cases
        ):
            raise LiveRunContractError("UNTRUSTED_TYPE", "smoke cases are untrusted")
        if tuple(case.mode for case in self.cases) != (
            SmokeModeV1.OFF,
            SmokeModeV1.SHADOW,
            SmokeModeV1.ACTIVE,
        ):
            raise LiveRunContractError("INCOMPLETE_SMOKE_MATRIX", "smoke needs OFF/SHADOW/ACTIVE")
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise LiveRunContractError("DUPLICATE_SMOKE_CASE", "smoke case IDs repeat")


@dataclass(frozen=True, slots=True)
class SequenceSafetyV1:
    stages: tuple[RunStageV1, ...]
    stop_on_failure: bool
    pilot_only_after_both_smokes_pass: bool
    default_dry_run: bool
    arbitrary_commands_forbidden: bool
    secrets_in_logs_forbidden: bool
    repo_external_output_required: bool

    def __post_init__(self) -> None:
        if self.stages != (
            RunStageV1.RESOURCE_PREFLIGHT,
            RunStageV1.QWEN_LIVE_SMOKE,
            RunStageV1.MAI_LIVE_SMOKE,
            RunStageV1.R25_PILOT,
        ):
            raise LiveRunContractError("INVALID_STAGE_ORDER", "sequence stage order is not exact")
        for field_name in (
            "stop_on_failure",
            "pilot_only_after_both_smokes_pass",
            "default_dry_run",
            "arbitrary_commands_forbidden",
            "secrets_in_logs_forbidden",
            "repo_external_output_required",
        ):
            _require_bool(getattr(self, field_name), field_name, True)


@dataclass(frozen=True, slots=True)
class R24R25RunAuthorityManifestV1:
    schema_version: str
    run_id: str
    source_commit: str
    authorization: OwnerAuthorizationV1
    safety: SequenceSafetyV1
    secret: SecretFileReferenceV1
    openai_stages: tuple[OpenAIResponsesStageV1, ...]
    actor_resources: tuple[SnapshotResourceV1, ...]
    smoke_plans: tuple[HostLiveSmokePlanV1, ...]
    pilot: FrozenPilotManifestV1
    topology_comparison_artifact_sha256: str
    output_root: str
    max_resource_preflight_wall_time_seconds: int
    max_sequence_wall_time_seconds: int
    max_sequence_openai_calls: int
    max_sequence_actor_calls: int
    max_sequence_cost_usd_micros: int

    def __post_init__(self) -> None:
        if self.schema_version != R24_R25_RUN_AUTHORITY_SCHEMA_VERSION:
            raise LiveRunContractError("UNKNOWN_SCHEMA", "unknown R2.4/R2.5 authority schema")
        _require_id(self.run_id, "run_id")
        if type(self.source_commit) is not str or _GIT_SHA1.fullmatch(self.source_commit) is None:
            raise LiveRunContractError(
                "INVALID_COMMIT", "source_commit must be full lowercase SHA-1"
            )
        if type(self.authorization) is not OwnerAuthorizationV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "authorization is untrusted")
        if type(self.safety) is not SequenceSafetyV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "safety declaration is untrusted")
        if type(self.secret) is not SecretFileReferenceV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "secret reference is untrusted")
        if type(self.openai_stages) is not tuple or tuple(
            stage.role for stage in self.openai_stages
        ) != (OpenAIRoleV1.RUBRIC, OpenAIRoleV1.HISTORY_POLICY):
            raise LiveRunContractError(
                "INVALID_OPENAI_MATRIX",
                "OpenAI stages must be independent rubric then history policy",
            )
        if any(type(stage) is not OpenAIResponsesStageV1 for stage in self.openai_stages):
            raise LiveRunContractError("UNTRUSTED_TYPE", "OpenAI stage is untrusted")
        if type(self.actor_resources) is not tuple or tuple(
            resource.host for resource in self.actor_resources
        ) != (PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI):
            raise LiveRunContractError("INVALID_HOST_MATRIX", "resources must be Qwen then MAI")
        if any(type(resource) is not SnapshotResourceV1 for resource in self.actor_resources):
            raise LiveRunContractError("UNTRUSTED_TYPE", "actor resource is untrusted")
        if type(self.smoke_plans) is not tuple or tuple(plan.host for plan in self.smoke_plans) != (
            PilotHostV1.QWEN3_VL,
            PilotHostV1.MAI_UI,
        ):
            raise LiveRunContractError("INVALID_HOST_MATRIX", "smokes must be Qwen then MAI")
        if any(type(plan) is not HostLiveSmokePlanV1 for plan in self.smoke_plans):
            raise LiveRunContractError("UNTRUSTED_TYPE", "smoke plan is untrusted")
        if type(self.pilot) is not FrozenPilotManifestV1:
            raise LiveRunContractError("UNTRUSTED_TYPE", "pilot is untrusted")
        _require_sha256(
            self.topology_comparison_artifact_sha256,
            "topology_comparison_artifact_sha256",
        )
        if (
            self.topology_comparison_artifact_sha256
            != self.pilot.topology_comparison_artifact_sha256
        ):
            raise LiveRunContractError(
                "TOPOLOGY_BINDING_MISMATCH",
                "run authority and frozen pilot bind different topology evidence",
            )
        _require_path(self.output_root, "output_root")
        _require_int(
            self.max_resource_preflight_wall_time_seconds,
            "max_resource_preflight_wall_time_seconds",
            1,
            86_400,
        )
        _require_int(
            self.max_sequence_wall_time_seconds,
            "max_sequence_wall_time_seconds",
            1,
            604_800,
        )
        smoke_actor = sum(case.max_actor_calls for plan in self.smoke_plans for case in plan.cases)
        smoke_openai = sum(
            case.max_openai_calls for plan in self.smoke_plans for case in plan.cases
        )
        smoke_cost = sum(
            case.max_cost_usd_micros for plan in self.smoke_plans for case in plan.cases
        )
        expected_actor = smoke_actor + self.pilot.max_total_actor_calls
        expected_openai = smoke_openai + self.pilot.max_total_openai_calls
        expected_cost = smoke_cost + self.pilot.max_total_cost_usd_micros
        if self.max_sequence_actor_calls != expected_actor:
            raise LiveRunContractError(
                "BUDGET_BINDING_MISMATCH", "sequence actor budget is not additive"
            )
        if self.max_sequence_openai_calls != expected_openai:
            raise LiveRunContractError(
                "BUDGET_BINDING_MISMATCH", "sequence OpenAI budget is not additive"
            )
        if self.max_sequence_cost_usd_micros != expected_cost:
            raise LiveRunContractError(
                "BUDGET_BINDING_MISMATCH", "sequence cost budget is not additive"
            )
        expected_time = (
            self.max_resource_preflight_wall_time_seconds
            + sum(case.max_wall_time_seconds for plan in self.smoke_plans for case in plan.cases)
            + self.pilot.max_total_wall_time_seconds
        )
        if self.max_sequence_wall_time_seconds != expected_time:
            raise LiveRunContractError(
                "BUDGET_BINDING_MISMATCH", "sequence time budget is not additive"
            )


def _authorization_projection(value: OwnerAuthorizationV1) -> dict[str, JsonValue]:
    if type(value) is not OwnerAuthorizationV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "authorization is untrusted")
    value = OwnerAuthorizationV1(
        **{field.name: getattr(value, field.name) for field in fields(OwnerAuthorizationV1)}
    )
    return {
        "actor_model_calls_allowed": value.actor_model_calls_allowed,
        "authorization_id": value.authorization_id,
        "authorized_by": value.authorized_by,
        "backend_allowed": value.backend_allowed,
        "docker_allowed": value.docker_allowed,
        "expires_at_utc": value.expires_at_utc,
        "frozen_artifact_mutation_allowed": value.frozen_artifact_mutation_allowed,
        "gpu_allowed": value.gpu_allowed,
        "issued_at_utc": value.issued_at_utc,
        "linear_update_allowed": value.linear_update_allowed,
        "merge_allowed": value.merge_allowed,
        "model_loading_allowed": value.model_loading_allowed,
        "network_allowed": value.network_allowed,
        "pilot_gui_actions_allowed": value.pilot_gui_actions_allowed,
        "sentinel_provider_calls_allowed": value.sentinel_provider_calls_allowed,
        "smoke_gui_actions_allowed": value.smoke_gui_actions_allowed,
        "status": value.status.value,
    }


def _secret_projection(value: SecretFileReferenceV1) -> dict[str, JsonValue]:
    if type(value) is not SecretFileReferenceV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "secret reference is untrusted")
    value = SecretFileReferenceV1(
        **{field.name: getattr(value, field.name) for field in fields(SecretFileReferenceV1)}
    )
    return {
        "content_may_be_read_by_preflight": value.content_may_be_read_by_preflight,
        "environment_key": value.environment_key,
        "path": value.path,
        "persist_value_or_hash": value.persist_value_or_hash,
        "required_mode": value.required_mode,
    }


def _openai_projection(value: OpenAIResponsesStageV1) -> dict[str, JsonValue]:
    if type(value) is not OpenAIResponsesStageV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "OpenAI stage is untrusted")
    value = OpenAIResponsesStageV1(
        **{field.name: getattr(value, field.name) for field in fields(OpenAIResponsesStageV1)}
    )
    return {
        "endpoint": value.endpoint,
        "external_network_on_call": value.external_network_on_call,
        "max_attempts": value.max_attempts,
        "max_output_tokens": value.max_output_tokens,
        "model": value.model,
        "model_on_call": value.model_on_call,
        "openai_sdk_version": value.openai_sdk_version,
        "role": value.role.value,
        "sdk_max_retries": value.sdk_max_retries,
        "store": value.store,
        "timeout_ms": value.timeout_ms,
        "transport_authority": value.transport_authority,
        "transport_kind": value.transport_kind,
    }


def _resource_projection(value: SnapshotResourceV1) -> dict[str, JsonValue]:
    if type(value) is not SnapshotResourceV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "actor resource is untrusted")
    value = SnapshotResourceV1(
        **{field.name: getattr(value, field.name) for field in fields(SnapshotResourceV1)}
    )
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


def _smoke_case_projection(value: LiveSmokeCaseV1) -> dict[str, JsonValue]:
    if type(value) is not LiveSmokeCaseV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "smoke case is untrusted")
    value = LiveSmokeCaseV1(
        **{field.name: getattr(value, field.name) for field in fields(LiveSmokeCaseV1)}
    )
    return {
        "actor_action_allowed": value.actor_action_allowed,
        "case_id": value.case_id,
        "max_actor_calls": value.max_actor_calls,
        "max_cost_usd_micros": value.max_cost_usd_micros,
        "max_openai_calls": value.max_openai_calls,
        "max_wall_time_seconds": value.max_wall_time_seconds,
        "mode": value.mode.value,
        "provider_final_request_proof_required": value.provider_final_request_proof_required,
        "request_fixture_byte_count": value.request_fixture_byte_count,
        "request_fixture_path": value.request_fixture_path,
        "request_fixture_sha256": value.request_fixture_sha256,
        "task_id": value.task_id,
    }


def _smoke_plan_projection(value: HostLiveSmokePlanV1) -> dict[str, JsonValue]:
    if type(value) is not HostLiveSmokePlanV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "smoke plan is untrusted")
    value = HostLiveSmokePlanV1(
        **{field.name: getattr(value, field.name) for field in fields(HostLiveSmokePlanV1)}
    )
    return {
        "cases": [cast(JsonValue, _smoke_case_projection(case)) for case in value.cases],
        "host": value.host.value,
    }


def _safety_projection(value: SequenceSafetyV1) -> dict[str, JsonValue]:
    if type(value) is not SequenceSafetyV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "sequence safety is untrusted")
    value = SequenceSafetyV1(
        **{field.name: getattr(value, field.name) for field in fields(SequenceSafetyV1)}
    )
    return {
        "arbitrary_commands_forbidden": value.arbitrary_commands_forbidden,
        "default_dry_run": value.default_dry_run,
        "pilot_only_after_both_smokes_pass": value.pilot_only_after_both_smokes_pass,
        "repo_external_output_required": value.repo_external_output_required,
        "secrets_in_logs_forbidden": value.secrets_in_logs_forbidden,
        "stages": [stage.value for stage in value.stages],
        "stop_on_failure": value.stop_on_failure,
    }


def authority_manifest_projection(value: R24R25RunAuthorityManifestV1) -> dict[str, JsonValue]:
    if type(value) is not R24R25RunAuthorityManifestV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "authority manifest is untrusted")
    trusted = R24R25RunAuthorityManifestV1(
        **{field.name: getattr(value, field.name) for field in fields(R24R25RunAuthorityManifestV1)}
    )
    return {
        "actor_resources": [
            cast(JsonValue, _resource_projection(resource)) for resource in trusted.actor_resources
        ],
        "authorization": cast(JsonValue, _authorization_projection(trusted.authorization)),
        "max_resource_preflight_wall_time_seconds": (
            trusted.max_resource_preflight_wall_time_seconds
        ),
        "max_sequence_actor_calls": trusted.max_sequence_actor_calls,
        "max_sequence_cost_usd_micros": trusted.max_sequence_cost_usd_micros,
        "max_sequence_openai_calls": trusted.max_sequence_openai_calls,
        "max_sequence_wall_time_seconds": trusted.max_sequence_wall_time_seconds,
        "openai_stages": [
            cast(JsonValue, _openai_projection(stage)) for stage in trusted.openai_stages
        ],
        "output_root": trusted.output_root,
        "pilot": cast(JsonValue, frozen_pilot_manifest_projection(trusted.pilot)),
        "run_id": trusted.run_id,
        "safety": cast(JsonValue, _safety_projection(trusted.safety)),
        "schema_version": trusted.schema_version,
        "secret": cast(JsonValue, _secret_projection(trusted.secret)),
        "smoke_plans": [
            cast(JsonValue, _smoke_plan_projection(plan)) for plan in trusted.smoke_plans
        ],
        "source_commit": trusted.source_commit,
        "topology_comparison_artifact_sha256": (trusted.topology_comparison_artifact_sha256),
    }


def authority_manifest_sha256(value: R24R25RunAuthorityManifestV1) -> str:
    return canonical_sha256(cast(JsonValue, authority_manifest_projection(value)))


_AUTHORIZATION_FIELDS = frozenset(
    {
        "actor_model_calls_allowed",
        "authorization_id",
        "authorized_by",
        "backend_allowed",
        "docker_allowed",
        "expires_at_utc",
        "frozen_artifact_mutation_allowed",
        "gpu_allowed",
        "issued_at_utc",
        "linear_update_allowed",
        "merge_allowed",
        "model_loading_allowed",
        "network_allowed",
        "pilot_gui_actions_allowed",
        "sentinel_provider_calls_allowed",
        "smoke_gui_actions_allowed",
        "status",
    }
)
_SECRET_FIELDS = frozenset(
    {
        "content_may_be_read_by_preflight",
        "environment_key",
        "path",
        "persist_value_or_hash",
        "required_mode",
    }
)
_OPENAI_FIELDS = frozenset(
    {
        "endpoint",
        "external_network_on_call",
        "max_attempts",
        "max_output_tokens",
        "model",
        "model_on_call",
        "openai_sdk_version",
        "role",
        "sdk_max_retries",
        "store",
        "timeout_ms",
        "transport_authority",
        "transport_kind",
    }
)
_RESOURCE_FIELDS = frozenset(
    {
        "actor_endpoint",
        "history_codec_id",
        "host",
        "host_enabled",
        "independent_kill_switch",
        "served_model_id",
        "snapshot_file_count",
        "snapshot_path",
        "snapshot_storage_root",
        "snapshot_total_bytes",
        "snapshot_tree_algorithm",
        "snapshot_tree_sha256",
    }
)
_SMOKE_CASE_FIELDS = frozenset(
    {
        "actor_action_allowed",
        "case_id",
        "max_actor_calls",
        "max_cost_usd_micros",
        "max_openai_calls",
        "max_wall_time_seconds",
        "mode",
        "provider_final_request_proof_required",
        "request_fixture_byte_count",
        "request_fixture_path",
        "request_fixture_sha256",
        "task_id",
    }
)
_SMOKE_PLAN_FIELDS = frozenset({"cases", "host"})
_SAFETY_FIELDS = frozenset(
    {
        "arbitrary_commands_forbidden",
        "default_dry_run",
        "pilot_only_after_both_smokes_pass",
        "repo_external_output_required",
        "secrets_in_logs_forbidden",
        "stages",
        "stop_on_failure",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "actor_resources",
        "authorization",
        "max_resource_preflight_wall_time_seconds",
        "max_sequence_actor_calls",
        "max_sequence_cost_usd_micros",
        "max_sequence_openai_calls",
        "max_sequence_wall_time_seconds",
        "openai_stages",
        "output_root",
        "pilot",
        "run_id",
        "safety",
        "schema_version",
        "secret",
        "smoke_plans",
        "source_commit",
        "topology_comparison_artifact_sha256",
    }
)


def _parse_authorization(value: object) -> OwnerAuthorizationV1:
    item = _exact_object(value, _AUTHORIZATION_FIELDS, "authorization")
    return OwnerAuthorizationV1(
        status=cast(
            RunAuthorizationStatusV1, _enum(RunAuthorizationStatusV1, item["status"], "status")
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
        pilot_gui_actions_allowed=cast(bool, item["pilot_gui_actions_allowed"]),
        smoke_gui_actions_allowed=cast(bool, item["smoke_gui_actions_allowed"]),
        merge_allowed=cast(bool, item["merge_allowed"]),
        linear_update_allowed=cast(bool, item["linear_update_allowed"]),
        frozen_artifact_mutation_allowed=cast(bool, item["frozen_artifact_mutation_allowed"]),
    )


def _parse_secret(value: object) -> SecretFileReferenceV1:
    item = _exact_object(value, _SECRET_FIELDS, "secret")
    return SecretFileReferenceV1(
        path=cast(str, item["path"]),
        environment_key=cast(str, item["environment_key"]),
        required_mode=cast(int, item["required_mode"]),
        content_may_be_read_by_preflight=cast(bool, item["content_may_be_read_by_preflight"]),
        persist_value_or_hash=cast(bool, item["persist_value_or_hash"]),
    )


def _parse_openai(value: object) -> OpenAIResponsesStageV1:
    item = _exact_object(value, _OPENAI_FIELDS, "OpenAI stage")
    return OpenAIResponsesStageV1(
        role=cast(OpenAIRoleV1, _enum(OpenAIRoleV1, item["role"], "OpenAI role")),
        model=cast(str, item["model"]),
        endpoint=cast(str, item["endpoint"]),
        transport_kind=cast(str, item["transport_kind"]),
        transport_authority=cast(str, item["transport_authority"]),
        openai_sdk_version=cast(str, item["openai_sdk_version"]),
        sdk_max_retries=cast(int, item["sdk_max_retries"]),
        external_network_on_call=cast(bool, item["external_network_on_call"]),
        model_on_call=cast(bool, item["model_on_call"]),
        max_output_tokens=cast(int, item["max_output_tokens"]),
        timeout_ms=cast(int, item["timeout_ms"]),
        max_attempts=cast(int, item["max_attempts"]),
        store=cast(bool, item["store"]),
    )


def _parse_resource(value: object) -> SnapshotResourceV1:
    item = _exact_object(value, _RESOURCE_FIELDS, "actor resource")
    return SnapshotResourceV1(
        host=cast(PilotHostV1, _enum(PilotHostV1, item["host"], "resource host")),
        history_codec_id=cast(str, item["history_codec_id"]),
        snapshot_path=cast(str, item["snapshot_path"]),
        snapshot_storage_root=cast(str, item["snapshot_storage_root"]),
        snapshot_tree_algorithm=cast(str, item["snapshot_tree_algorithm"]),
        snapshot_tree_sha256=cast(str, item["snapshot_tree_sha256"]),
        snapshot_total_bytes=cast(int, item["snapshot_total_bytes"]),
        snapshot_file_count=cast(int, item["snapshot_file_count"]),
        actor_endpoint=cast(str, item["actor_endpoint"]),
        served_model_id=cast(str, item["served_model_id"]),
        host_enabled=cast(bool, item["host_enabled"]),
        independent_kill_switch=cast(bool, item["independent_kill_switch"]),
    )


def _parse_smoke_case(value: object) -> LiveSmokeCaseV1:
    item = _exact_object(value, _SMOKE_CASE_FIELDS, "smoke case")
    return LiveSmokeCaseV1(
        case_id=cast(str, item["case_id"]),
        task_id=cast(str, item["task_id"]),
        mode=cast(SmokeModeV1, _enum(SmokeModeV1, item["mode"], "smoke mode")),
        request_fixture_path=cast(str, item["request_fixture_path"]),
        request_fixture_sha256=cast(str, item["request_fixture_sha256"]),
        request_fixture_byte_count=cast(int, item["request_fixture_byte_count"]),
        max_actor_calls=cast(int, item["max_actor_calls"]),
        max_openai_calls=cast(int, item["max_openai_calls"]),
        max_wall_time_seconds=cast(int, item["max_wall_time_seconds"]),
        max_cost_usd_micros=cast(int, item["max_cost_usd_micros"]),
        actor_action_allowed=cast(bool, item["actor_action_allowed"]),
        provider_final_request_proof_required=cast(
            bool, item["provider_final_request_proof_required"]
        ),
    )


def _parse_smoke_plan(value: object) -> HostLiveSmokePlanV1:
    item = _exact_object(value, _SMOKE_PLAN_FIELDS, "smoke plan")
    raw_cases = item["cases"]
    if type(raw_cases) is not list:
        raise LiveRunContractError("UNTRUSTED_TYPE", "smoke cases must be an array")
    return HostLiveSmokePlanV1(
        host=cast(PilotHostV1, _enum(PilotHostV1, item["host"], "smoke host")),
        cases=tuple(_parse_smoke_case(case) for case in cast(list[object], raw_cases)),
    )


def _parse_safety(value: object) -> SequenceSafetyV1:
    item = _exact_object(value, _SAFETY_FIELDS, "safety")
    raw_stages = item["stages"]
    if type(raw_stages) is not list:
        raise LiveRunContractError("UNTRUSTED_TYPE", "stages must be an array")
    return SequenceSafetyV1(
        stages=tuple(
            cast(RunStageV1, _enum(RunStageV1, stage, "run stage"))
            for stage in cast(list[object], raw_stages)
        ),
        stop_on_failure=cast(bool, item["stop_on_failure"]),
        pilot_only_after_both_smokes_pass=cast(bool, item["pilot_only_after_both_smokes_pass"]),
        default_dry_run=cast(bool, item["default_dry_run"]),
        arbitrary_commands_forbidden=cast(bool, item["arbitrary_commands_forbidden"]),
        secrets_in_logs_forbidden=cast(bool, item["secrets_in_logs_forbidden"]),
        repo_external_output_required=cast(bool, item["repo_external_output_required"]),
    )


def parse_authority_manifest(value: object) -> R24R25RunAuthorityManifestV1:
    item = _exact_object(value, _MANIFEST_FIELDS, "authority manifest")
    raw_openai = item["openai_stages"]
    raw_resources = item["actor_resources"]
    raw_smokes = item["smoke_plans"]
    if any(type(value) is not list for value in (raw_openai, raw_resources, raw_smokes)):
        raise LiveRunContractError("UNTRUSTED_TYPE", "manifest collections must be arrays")
    try:
        pilot = parse_frozen_pilot_manifest(item["pilot"])
    except R25PilotContractError as exc:
        raise LiveRunContractError("INVALID_PILOT", "embedded frozen pilot is invalid") from exc
    return R24R25RunAuthorityManifestV1(
        schema_version=cast(str, item["schema_version"]),
        run_id=cast(str, item["run_id"]),
        source_commit=cast(str, item["source_commit"]),
        authorization=_parse_authorization(item["authorization"]),
        safety=_parse_safety(item["safety"]),
        secret=_parse_secret(item["secret"]),
        openai_stages=tuple(_parse_openai(value) for value in cast(list[object], raw_openai)),
        actor_resources=tuple(
            _parse_resource(value) for value in cast(list[object], raw_resources)
        ),
        smoke_plans=tuple(_parse_smoke_plan(value) for value in cast(list[object], raw_smokes)),
        pilot=pilot,
        topology_comparison_artifact_sha256=cast(str, item["topology_comparison_artifact_sha256"]),
        output_root=cast(str, item["output_root"]),
        max_resource_preflight_wall_time_seconds=cast(
            int, item["max_resource_preflight_wall_time_seconds"]
        ),
        max_sequence_wall_time_seconds=cast(int, item["max_sequence_wall_time_seconds"]),
        max_sequence_openai_calls=cast(int, item["max_sequence_openai_calls"]),
        max_sequence_actor_calls=cast(int, item["max_sequence_actor_calls"]),
        max_sequence_cost_usd_micros=cast(int, item["max_sequence_cost_usd_micros"]),
    )


def _reject_duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LiveRunContractError("DUPLICATE_JSON_KEY", "manifest repeats a JSON key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise LiveRunContractError("NON_CANONICAL_JSON", "manifest contains a non-finite number")


def load_authority_manifest(path: Path) -> R24R25RunAuthorityManifestV1:
    try:
        invalid_file = path.is_symlink() or not path.is_file()
        size = path.stat().st_size
    except OSError as exc:
        raise LiveRunContractError(
            "INVALID_MANIFEST_FILE", "manifest metadata is unavailable"
        ) from exc
    if invalid_file:
        raise LiveRunContractError("INVALID_MANIFEST_FILE", "manifest must be a regular file")
    if not 1 <= size <= _MAX_MANIFEST_BYTES:
        raise LiveRunContractError("INVALID_MANIFEST_FILE", "manifest size is outside the bound")
    try:
        raw = path.read_bytes()
        decoded = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except LiveRunContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LiveRunContractError("INVALID_MANIFEST_FILE", "manifest is not strict JSON") from exc
    return parse_authority_manifest(decoded)


@dataclass(frozen=True, slots=True)
class SnapshotTreeDigestV1:
    sha256: str
    total_bytes: int
    file_count: int

    def __post_init__(self) -> None:
        _require_sha256(self.sha256, "snapshot digest")
        _require_int(self.total_bytes, "snapshot digest bytes", 1, 10_000_000_000_000)
        _require_int(self.file_count, "snapshot digest files", 1, 1_000_000)


def compute_snapshot_tree_digest(resource: SnapshotResourceV1) -> SnapshotTreeDigestV1:
    """Hash declared model files without loading a model or touching accelerators."""

    if type(resource) is not SnapshotResourceV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "snapshot resource is untrusted")
    logical_root = Path(resource.snapshot_path)
    storage_root = Path(resource.snapshot_storage_root).resolve(strict=True)
    root = logical_root.resolve(strict=True)
    if not root.is_dir() or not storage_root.is_dir() or not _is_within(root, storage_root):
        raise LiveRunContractError("INVALID_SNAPSHOT_ROOT", "snapshot is outside its storage root")
    files: list[tuple[str, Path]] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory_name in directory_names:
            if (current_path / directory_name).is_symlink():
                raise LiveRunContractError(
                    "INVALID_SNAPSHOT_TREE", "snapshot has a symlink directory"
                )
        for file_name in file_names:
            logical_path = current_path / file_name
            target = logical_path.resolve(strict=True)
            if not target.is_file() or not _is_within(target, storage_root):
                raise LiveRunContractError(
                    "INVALID_SNAPSHOT_TREE", "snapshot file escapes storage root"
                )
            relative = logical_path.relative_to(root).as_posix()
            files.append((relative, target))
            if len(files) > 1_000_000:
                raise LiveRunContractError("SNAPSHOT_TOO_LARGE", "snapshot has too many files")
    files.sort(key=lambda item: item[0].encode("utf-8"))
    if not files:
        raise LiveRunContractError("INVALID_SNAPSHOT_TREE", "snapshot tree is empty")
    aggregate = hashlib.sha256()
    aggregate.update(b"mobileworld.snapshot.logical-tree/v1\0")
    total_bytes = 0
    for relative, target in files:
        file_hash = hashlib.sha256()
        byte_count = 0
        try:
            with target.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    file_hash.update(chunk)
                    byte_count += len(chunk)
        except OSError as exc:
            raise LiveRunContractError(
                "SNAPSHOT_READ_FAILED", "snapshot file could not be read"
            ) from exc
        relative_bytes = relative.encode("utf-8")
        aggregate.update(len(relative_bytes).to_bytes(8, "big"))
        aggregate.update(relative_bytes)
        aggregate.update(byte_count.to_bytes(16, "big"))
        aggregate.update(file_hash.digest())
        total_bytes += byte_count
    return SnapshotTreeDigestV1(aggregate.hexdigest(), total_bytes, len(files))


@dataclass(frozen=True, slots=True)
class ResourceCheckV1:
    check_id: str
    passed: bool
    verification: str

    def __post_init__(self) -> None:
        _require_id(self.check_id, "check_id")
        if type(self.passed) is not bool:
            raise LiveRunContractError("UNTRUSTED_TYPE", "resource check status is untrusted")
        if self.verification not in {"METADATA", "CONTENT_SHA256", "DECLARATION"}:
            raise LiveRunContractError("INVALID_VERIFICATION", "resource verification is unknown")


@dataclass(frozen=True, slots=True)
class ResourcePreflightReportV1:
    schema_version: str
    run_id: str
    manifest_sha256: str
    checks: tuple[ResourceCheckV1, ...]
    owner_authority_present: bool
    authority_current: bool
    deep_snapshot_hashes_verified: bool
    ready_for_production_execution: bool
    production_executor_installed: bool
    secret_content_read: bool
    network_calls: int
    gpu_operations: int
    docker_operations: int
    model_loads: int
    backend_operations: int
    actor_actions: int
    files_written: int

    def __post_init__(self) -> None:
        if self.schema_version != R24_R25_PREFLIGHT_SCHEMA_VERSION:
            raise LiveRunContractError("UNKNOWN_SCHEMA", "unknown preflight schema")
        _require_id(self.run_id, "run_id")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if (
            type(self.checks) is not tuple
            or not self.checks
            or any(type(check) is not ResourceCheckV1 for check in self.checks)
        ):
            raise LiveRunContractError("UNTRUSTED_TYPE", "preflight checks are untrusted")
        for name in (
            "owner_authority_present",
            "authority_current",
            "deep_snapshot_hashes_verified",
            "ready_for_production_execution",
            "production_executor_installed",
            "secret_content_read",
        ):
            if type(getattr(self, name)) is not bool:
                raise LiveRunContractError("UNTRUSTED_TYPE", "preflight flag is untrusted")
        if self.ready_for_production_execution:
            raise LiveRunContractError(
                "PREFLIGHT_NOT_EXECUTION_AUTHORITY",
                "CPU preflight alone cannot claim execution readiness",
            )
        if self.secret_content_read:
            raise LiveRunContractError(
                "SECRET_BOUNDARY_VIOLATION", "preflight may not read secret content"
            )
        for name in (
            "network_calls",
            "gpu_operations",
            "docker_operations",
            "model_loads",
            "backend_operations",
            "actor_actions",
            "files_written",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) != 0:
                raise LiveRunContractError(
                    "PREFLIGHT_SIDE_EFFECT", "dry-run side-effect census is nonzero"
                )


def _file_matches(path: Path, sha256: str, byte_count: int) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size != byte_count:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest() == sha256
    except OSError:
        return False


def _pilot_task_source_matches(manifest: FrozenPilotManifestV1, *, repository_root: Path) -> bool:
    """Admit only the executable, parameter-bound pilot task source.

    The legacy hash-only projection remains available to old CPU fixtures, but
    it is intentionally not sufficient for an R2.4/R2.5 run authority.
    """

    try:
        resolve_pilot_task_inputs_v1(
            manifest,
            authorized_input_root=Path(manifest.task_manifest_path).parent,
            repository_root=repository_root,
        )
    except (OSError, R25PilotContractError, RecursionError):
        return False
    return True


def _git_state(repo_root: Path) -> tuple[str | None, bool]:
    environment = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    head = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "rev-parse",
            "HEAD",
        ],
        cwd=repo_root,
        env=environment,
        check=False,
        capture_output=True,
    )
    status = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        cwd=repo_root,
        env=environment,
        check=False,
        capture_output=True,
    )
    if head.returncode != 0 or head.stderr or status.returncode != 0 or status.stderr:
        return None, False
    return head.stdout.decode("ascii", errors="strict").strip(), not status.stdout


def inspect_local_resources(
    manifest: R24R25RunAuthorityManifestV1,
    *,
    repo_root: Path,
    deep_snapshot_hashes: bool = False,
    now: datetime | None = None,
) -> ResourcePreflightReportV1:
    """Read only metadata and declared non-secret artifacts; never read the key file."""

    if type(manifest) is not R24R25RunAuthorityManifestV1 or not isinstance(repo_root, Path):
        raise LiveRunContractError("UNTRUSTED_TYPE", "preflight inputs are untrusted")
    trusted_root = repo_root.resolve(strict=True)
    if not trusted_root.is_dir():
        raise LiveRunContractError("INVALID_REPOSITORY", "repository root is not a directory")
    checks: list[ResourceCheckV1] = []
    head, clean = _git_state(trusted_root)
    checks.append(ResourceCheckV1("git_source_commit", head == manifest.source_commit, "METADATA"))
    checks.append(ResourceCheckV1("git_worktree_clean", clean, "METADATA"))

    secret_path = Path(manifest.secret.path)
    secret_passed = False
    try:
        secret_stat = secret_path.lstat()
        secret_passed = (
            stat.S_ISREG(secret_stat.st_mode)
            and not secret_path.is_symlink()
            and stat.S_IMODE(secret_stat.st_mode) == 0o600
            and secret_stat.st_uid == os.geteuid()
            and secret_stat.st_gid == os.getegid()
            and secret_stat.st_nlink == 1
            and 0 < secret_stat.st_size <= 65_536
            and not _is_within(secret_path.resolve(strict=True), trusted_root)
        )
    except OSError:
        secret_passed = False
    checks.append(ResourceCheckV1("openai_secret_external_regular_0600", secret_passed, "METADATA"))

    output = Path(manifest.output_root)
    try:
        output_resolved = output.resolve(strict=False)
        output_passed = (
            not output.exists()
            and not output.is_symlink()
            and output.parent.resolve(strict=True).is_dir()
            and not _is_within(output_resolved, trusted_root)
        )
    except OSError:
        output_passed = False
    checks.append(ResourceCheckV1("fresh_repo_external_output_root", output_passed, "METADATA"))

    pilot_manifest_ok = _pilot_task_source_matches(manifest.pilot, repository_root=trusted_root)
    checks.append(
        ResourceCheckV1("frozen_pilot_task_manifest", pilot_manifest_ok, "CONTENT_SHA256")
    )
    checks.append(
        ResourceCheckV1(
            "frozen_cpu_topology_comparison",
            _file_matches(
                Path(manifest.pilot.topology_comparison_artifact_path),
                manifest.topology_comparison_artifact_sha256,
                manifest.pilot.topology_comparison_artifact_byte_count,
            ),
            "CONTENT_SHA256",
        )
    )
    for plan in manifest.smoke_plans:
        for case in plan.cases:
            checks.append(
                ResourceCheckV1(
                    f"smoke_fixture:{plan.host.value}:{case.mode.value}",
                    _file_matches(
                        Path(case.request_fixture_path),
                        case.request_fixture_sha256,
                        case.request_fixture_byte_count,
                    ),
                    "CONTENT_SHA256",
                )
            )
    for resource in manifest.actor_resources:
        resource_ok = False
        try:
            snapshot = Path(resource.snapshot_path).resolve(strict=True)
            storage = Path(resource.snapshot_storage_root).resolve(strict=True)
            resource_ok = snapshot.is_dir() and storage.is_dir() and _is_within(snapshot, storage)
        except OSError:
            resource_ok = False
        checks.append(
            ResourceCheckV1(f"snapshot_metadata:{resource.host.value}", resource_ok, "METADATA")
        )
        if deep_snapshot_hashes and resource_ok:
            digest = compute_snapshot_tree_digest(resource)
            matches = (
                digest.sha256 == resource.snapshot_tree_sha256
                and digest.total_bytes == resource.snapshot_total_bytes
                and digest.file_count == resource.snapshot_file_count
            )
            checks.append(
                ResourceCheckV1(
                    f"snapshot_content:{resource.host.value}", matches, "CONTENT_SHA256"
                )
            )
        else:
            checks.append(
                ResourceCheckV1(f"snapshot_content:{resource.host.value}", False, "DECLARATION")
            )
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise LiveRunContractError("INVALID_TIMESTAMP", "preflight current time must be aware")
    authority_present = manifest.authorization.status is RunAuthorizationStatusV1.OWNER_AUTHORIZED
    authority_current = authority_present and _timestamp(
        manifest.authorization.issued_at_utc
    ) <= current_time.astimezone(UTC) < _timestamp(manifest.authorization.expires_at_utc)
    return ResourcePreflightReportV1(
        schema_version=R24_R25_PREFLIGHT_SCHEMA_VERSION,
        run_id=manifest.run_id,
        manifest_sha256=authority_manifest_sha256(manifest),
        checks=tuple(checks),
        owner_authority_present=authority_present,
        authority_current=authority_current,
        deep_snapshot_hashes_verified=deep_snapshot_hashes
        and all(check.passed for check in checks if check.check_id.startswith("snapshot_content:")),
        ready_for_production_execution=False,
        production_executor_installed=True,
        secret_content_read=False,
        network_calls=0,
        gpu_operations=0,
        docker_operations=0,
        model_loads=0,
        backend_operations=0,
        actor_actions=0,
        files_written=0,
    )


def preflight_report_projection(value: ResourcePreflightReportV1) -> dict[str, JsonValue]:
    if type(value) is not ResourcePreflightReportV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "preflight report is untrusted")
    return {
        "actor_actions": value.actor_actions,
        "authority_current": value.authority_current,
        "backend_operations": value.backend_operations,
        "checks": [
            {"check_id": check.check_id, "passed": check.passed, "verification": check.verification}
            for check in value.checks
        ],
        "deep_snapshot_hashes_verified": value.deep_snapshot_hashes_verified,
        "docker_operations": value.docker_operations,
        "files_written": value.files_written,
        "gpu_operations": value.gpu_operations,
        "manifest_sha256": value.manifest_sha256,
        "model_loads": value.model_loads,
        "network_calls": value.network_calls,
        "owner_authority_present": value.owner_authority_present,
        "production_executor_installed": value.production_executor_installed,
        "ready_for_production_execution": value.ready_for_production_execution,
        "run_id": value.run_id,
        "schema_version": value.schema_version,
        "secret_content_read": value.secret_content_read,
    }


@dataclass(frozen=True, slots=True)
class StageExecutionReceiptV1:
    stage: RunStageV1
    manifest_sha256: str
    passed: bool
    evidence_sha256: str
    actor_calls: int
    openai_calls: int
    actor_actions: int
    cost_usd_micros: int
    wall_time_ms: int
    completed_units: tuple[str, ...]
    provider_final_request_proven: bool

    def __post_init__(self) -> None:
        if type(self.stage) is not RunStageV1 or type(self.passed) is not bool:
            raise LiveRunContractError("UNTRUSTED_TYPE", "stage receipt is untrusted")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        _require_sha256(self.evidence_sha256, "evidence_sha256")
        for value, name, maximum in (
            (self.actor_calls, "actor_calls", 1_000_000),
            (self.openai_calls, "openai_calls", 2_000_000),
            (self.actor_actions, "actor_actions", 1_000_000),
            (self.cost_usd_micros, "cost_usd_micros", 100_000_000_000),
            (self.wall_time_ms, "wall_time_ms", 604_800_000),
        ):
            _require_int(value, name, 0, maximum)
        if (
            type(self.completed_units) is not tuple
            or len(self.completed_units) > 200
            or any(
                type(unit) is not str or _ID.fullmatch(unit) is None
                for unit in self.completed_units
            )
        ):
            raise LiveRunContractError("INVALID_STAGE_UNITS", "stage units are invalid")
        if len(set(self.completed_units)) != len(self.completed_units):
            raise LiveRunContractError("INVALID_STAGE_UNITS", "stage units repeat")
        if type(self.provider_final_request_proven) is not bool:
            raise LiveRunContractError("UNTRUSTED_TYPE", "provider proof flag is untrusted")


@dataclass(frozen=True, slots=True)
class SequenceRunResultV1:
    schema_version: str
    run_id: str
    manifest_sha256: str
    status: SequenceStatusV1
    receipts: tuple[StageExecutionReceiptV1, ...]
    failed_stage: RunStageV1 | None
    failure_code: str | None

    def __post_init__(self) -> None:
        if self.schema_version != R24_R25_SEQUENCE_RESULT_SCHEMA_VERSION:
            raise LiveRunContractError("UNKNOWN_SCHEMA", "unknown sequence-result schema")
        _require_id(self.run_id, "run_id")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        if type(self.status) is not SequenceStatusV1 or type(self.receipts) is not tuple:
            raise LiveRunContractError("UNTRUSTED_TYPE", "sequence result is untrusted")
        if any(type(receipt) is not StageExecutionReceiptV1 for receipt in self.receipts):
            raise LiveRunContractError("UNTRUSTED_TYPE", "sequence receipt is untrusted")
        if self.status is SequenceStatusV1.COMPLETE:
            if self.failed_stage is not None or self.failure_code is not None:
                raise LiveRunContractError("INVALID_SEQUENCE_RESULT", "complete result has failure")
        elif (
            type(self.failed_stage) is not RunStageV1
            or type(self.failure_code) is not str
            or not self.failure_code
        ):
            raise LiveRunContractError(
                "INVALID_SEQUENCE_RESULT", "failed result needs typed failure"
            )


@runtime_checkable
class SequenceStageExecutorV1(Protocol):
    """CPU-test seam only until a module-owned production executor exists."""

    def run_stage(
        self, stage: RunStageV1, manifest: R24R25RunAuthorityManifestV1
    ) -> StageExecutionReceiptV1: ...


def _expected_units(stage: RunStageV1, manifest: R24R25RunAuthorityManifestV1) -> tuple[str, ...]:
    if stage is RunStageV1.RESOURCE_PREFLIGHT:
        return ("resources",)
    if stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}:
        host = PilotHostV1.QWEN3_VL if stage is RunStageV1.QWEN_LIVE_SMOKE else PilotHostV1.MAI_UI
        plan = next(plan for plan in manifest.smoke_plans if plan.host is host)
        return tuple(f"{host.value}:{case.mode.value}" for case in plan.cases)
    return tuple(f"pilot-cell-{index:03d}" for index, _ in enumerate(manifest.pilot.cells))


def _receipt_within_stage_bounds(
    receipt: StageExecutionReceiptV1,
    manifest: R24R25RunAuthorityManifestV1,
) -> bool:
    if receipt.manifest_sha256 != authority_manifest_sha256(manifest):
        return False
    if receipt.completed_units != _expected_units(receipt.stage, manifest):
        return False
    if receipt.stage is RunStageV1.RESOURCE_PREFLIGHT:
        return (
            receipt.actor_calls == 0
            and receipt.openai_calls == 0
            and receipt.actor_actions == 0
            and receipt.cost_usd_micros == 0
            and receipt.wall_time_ms <= manifest.max_resource_preflight_wall_time_seconds * 1000
            and not receipt.provider_final_request_proven
        )
    if receipt.stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}:
        host = (
            PilotHostV1.QWEN3_VL
            if receipt.stage is RunStageV1.QWEN_LIVE_SMOKE
            else PilotHostV1.MAI_UI
        )
        plan = next(plan for plan in manifest.smoke_plans if plan.host is host)
        minimum_openai_calls = sum(0 if case.mode is SmokeModeV1.OFF else 2 for case in plan.cases)
        maximum_openai_calls = sum(case.max_openai_calls for case in plan.cases)
        return (
            receipt.actor_calls == sum(case.max_actor_calls for case in plan.cases)
            and minimum_openai_calls <= receipt.openai_calls <= maximum_openai_calls
            and receipt.cost_usd_micros <= sum(case.max_cost_usd_micros for case in plan.cases)
            and receipt.wall_time_ms
            <= sum(case.max_wall_time_seconds for case in plan.cases) * 1000
            and receipt.actor_actions == 0
            and receipt.provider_final_request_proven
        )
    return (
        len(manifest.pilot.cells) <= receipt.actor_calls <= manifest.pilot.max_total_actor_calls
        and receipt.openai_calls <= manifest.pilot.max_total_openai_calls
        and receipt.cost_usd_micros <= manifest.pilot.max_total_cost_usd_micros
        and receipt.wall_time_ms <= manifest.pilot.max_total_wall_time_seconds * 1000
        and receipt.actor_actions <= receipt.actor_calls
        and receipt.provider_final_request_proven
    )


def run_authorized_sequence_with_executor(
    manifest: R24R25RunAuthorityManifestV1,
    executor: SequenceStageExecutorV1,
    *,
    confirmed_manifest_sha256: str,
    now: datetime | None = None,
) -> SequenceRunResultV1:
    """Exercise the stop-on-failure state machine with an injected executor.

    This API exists so CPU tests can prove ordering and gating.  The public CLI
    does not accept an executor and remains non-executable until a trusted
    production implementation is checked in.
    """

    if type(manifest) is not R24R25RunAuthorityManifestV1:
        raise LiveRunContractError("UNTRUSTED_TYPE", "manifest is untrusted")
    manifest_hash = authority_manifest_sha256(manifest)
    if confirmed_manifest_sha256 != manifest_hash:
        raise LiveRunContractError(
            "MANIFEST_CONFIRMATION_MISMATCH", "confirmed manifest hash differs"
        )
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if manifest.authorization.status is not RunAuthorizationStatusV1.OWNER_AUTHORIZED:
        raise LiveRunContractError("OWNER_AUTHORITY_REQUIRED", "manifest is not owner-authorized")
    if not (
        _timestamp(manifest.authorization.issued_at_utc)
        <= current
        < _timestamp(manifest.authorization.expires_at_utc)
    ):
        raise LiveRunContractError("OWNER_AUTHORITY_EXPIRED", "owner authority is not current")
    if not isinstance(executor, SequenceStageExecutorV1):
        raise LiveRunContractError("EXECUTOR_REQUIRED", "stage executor protocol is absent")
    receipts: list[StageExecutionReceiptV1] = []
    for stage in manifest.safety.stages:
        try:
            receipt = executor.run_stage(stage, manifest)
            if type(receipt) is not StageExecutionReceiptV1 or receipt.stage is not stage:
                raise LiveRunContractError(
                    "INVALID_STAGE_RECEIPT", "executor returned another stage"
                )
            if not receipt.passed or not _receipt_within_stage_bounds(receipt, manifest):
                raise LiveRunContractError("STAGE_FAILED", "stage failed or exceeded its bound")
            receipts.append(receipt)
            if sum(item.actor_calls for item in receipts) > manifest.max_sequence_actor_calls:
                raise LiveRunContractError("SEQUENCE_BUDGET_EXCEEDED", "actor call budget exceeded")
            if sum(item.openai_calls for item in receipts) > manifest.max_sequence_openai_calls:
                raise LiveRunContractError(
                    "SEQUENCE_BUDGET_EXCEEDED", "OpenAI call budget exceeded"
                )
            if (
                sum(item.cost_usd_micros for item in receipts)
                > manifest.max_sequence_cost_usd_micros
            ):
                raise LiveRunContractError("SEQUENCE_BUDGET_EXCEEDED", "cost budget exceeded")
            if (
                sum(item.wall_time_ms for item in receipts)
                > manifest.max_sequence_wall_time_seconds * 1000
            ):
                raise LiveRunContractError("SEQUENCE_BUDGET_EXCEEDED", "time budget exceeded")
        except Exception as exc:
            code = exc.code if type(exc) is LiveRunContractError else "STAGE_EXECUTOR_ERROR"
            return SequenceRunResultV1(
                schema_version=R24_R25_SEQUENCE_RESULT_SCHEMA_VERSION,
                run_id=manifest.run_id,
                manifest_sha256=manifest_hash,
                status=SequenceStatusV1.FAILED,
                receipts=tuple(receipts),
                failed_stage=stage,
                failure_code=code,
            )
    return SequenceRunResultV1(
        schema_version=R24_R25_SEQUENCE_RESULT_SCHEMA_VERSION,
        run_id=manifest.run_id,
        manifest_sha256=manifest_hash,
        status=SequenceStatusV1.COMPLETE,
        receipts=tuple(receipts),
        failed_stage=None,
        failure_code=None,
    )


def require_production_executor() -> None:
    """Verify that the exact dependency-injected production executor is installed.

    This compatibility gate grants no execution authority.  Callers still need
    the owner-pinned manifest, sealed deep preflight, pricing/config hashes and
    the exact post-preflight factory before they can construct an executor.
    """

    from mobile_world.runtime.sentinel.r2_4.live_executor import (
        production_executor_available_v1,
    )

    if production_executor_available_v1() is not True:
        raise LiveRunContractError(
            "PRODUCTION_EXECUTOR_NOT_INSTALLED",
            "the exact production executor implementation is unavailable",
        )


__all__ = [
    "R24_R25_PREFLIGHT_SCHEMA_VERSION",
    "R24_R25_RUN_AUTHORITY_SCHEMA_VERSION",
    "R24_R25_SEQUENCE_RESULT_SCHEMA_VERSION",
    "SNAPSHOT_TREE_ALGORITHM_V1",
    "HostLiveSmokePlanV1",
    "LiveRunContractError",
    "LiveSmokeCaseV1",
    "OpenAIRoleV1",
    "OpenAIResponsesStageV1",
    "OwnerAuthorizationV1",
    "R24R25RunAuthorityManifestV1",
    "ResourceCheckV1",
    "ResourcePreflightReportV1",
    "RunAuthorizationStatusV1",
    "RunStageV1",
    "SecretFileReferenceV1",
    "SequenceRunResultV1",
    "SequenceSafetyV1",
    "SequenceStageExecutorV1",
    "SequenceStatusV1",
    "SmokeModeV1",
    "SnapshotResourceV1",
    "SnapshotTreeDigestV1",
    "StageExecutionReceiptV1",
    "authority_manifest_projection",
    "authority_manifest_sha256",
    "compute_snapshot_tree_digest",
    "frozen_pilot_manifest_sha256",
    "inspect_local_resources",
    "load_authority_manifest",
    "parse_authority_manifest",
    "preflight_report_projection",
    "require_production_executor",
    "run_authorized_sequence_with_executor",
]
