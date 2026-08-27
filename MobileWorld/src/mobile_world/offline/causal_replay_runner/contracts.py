"""Versioned CPU-only contracts for the G1 exact-request replay runner.

These records wrap the frozen G1.1 run semantics and the accepted G1.2
provider/result types.  They deliberately do not confer permission to contact a
provider.  A future live execution must present a separately frozen run-ready
seal; ALE-322's CPU conformance domain never produces one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    JsonValue,
    ProviderResult,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
)

PROTOCOL_VERSION = "mobileworld.g1.causal-replay/protocol-v1"
RUNNER_CONTRACT_VERSION = "mobileworld.g1.exact-request-replay/contract-v1"
RUNNER_VERSION = "mobileworld.g1.exact-request-replay-runner/v1"
INVOCATION_PLAN_SCHEMA_VERSION = "mobileworld.g1.replay-invocation-plan/v1"
INVARIANCE_SCHEMA_VERSION = "mobileworld.g1.replay-invariance-report/v1"
PROVIDER_EXCHANGE_SCHEMA_VERSION = "mobileworld.g1.replay-provider-exchange/v1"
ATTEMPT_EVENT_SCHEMA_VERSION = "mobileworld.g1.replay-attempt-event/v1"
TERMINAL_ATTEMPT_SCHEMA_VERSION = "mobileworld.g1.replay-terminal-attempt/v1"
BLINDED_PACKET_SCHEMA_VERSION = "mobileworld.g1.blinded-action-packet/v1"
BLINDING_MAPPING_SCHEMA_VERSION = "mobileworld.g1.confidential-blinding-mapping/v1"
BLINDED_PACKET_BINDING_SCHEMA_VERSION = "mobileworld.g1.confidential-blinded-packet-binding/v1"
CPU_MANIFEST_SCHEMA_VERSION = "mobileworld.g1.replay-runner-cpu-manifest/v1"

ARM_ORDER_SALT = "mobileworld-g1-arm-order-v1-20260826"
REPLAY_SEEDS = (1729, 2718, 31415)
REPEATS = (1, 2)
STRICT_ARMS = (
    ArmKind.ORIGINAL,
    ArmKind.MASK,
    ArmKind.MASK_CORRECTION,
    ArmKind.ORACLE_CLEAN,
    ArmKind.SHAM_BENIGN_EDIT,
)
CLEAN_ARMS = (ArmKind.ORIGINAL, ArmKind.SHAM_BENIGN_EDIT)
RETRYABLE_FAILURES = ("TIMEOUT", "HTTP_5XX", "CONNECTION_ERROR")
MAXIMUM_PROVIDER_ATTEMPTS = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^g1run-[0-9a-f]{24}$")
_CAPSULE_ID_RE = re.compile(r"^g1capsule-[0-9a-f]{24}$")
_UNIT_ID_RE = re.compile(r"^g1(?:case|control)-[0-9a-f]{24}$")
_SCHEDULE_ID_RE = re.compile(r"^g1schedule-[0-9a-f]{24}$")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


class ReplayRunnerError(RuntimeError):
    """Stable fail-closed error; never an invocation authorization."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        json_path: str | None = None,
        context: dict[str, JsonValue] | None = None,
    ) -> None:
        self.code = code
        self.json_path = json_path
        self.context = context or {}
        self.provider_invocation_allowed = False
        detail = f"{code}: {message}"
        if json_path is not None:
            detail += f" at {json_path}"
        super().__init__(detail)


class ExecutionDomain(str, Enum):
    FAKE_CONFORMANCE = "FAKE_CONFORMANCE"
    LIVE_G1_SCIENTIFIC = "LIVE_G1_SCIENTIFIC"


class UnitKind(str, Enum):
    STRICT_MHR = "STRICT_MHR"
    CLEAN_CONTROL = "CLEAN_CONTROL"


class AttemptEventKind(str, Enum):
    PLANNED = "PLANNED"
    PREFLIGHT_ALLOWED = "PREFLIGHT_ALLOWED"
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    CHUNK = "CHUNK"
    RETURNED = "RETURNED"
    FAILED = "FAILED"
    PARSED = "PARSED"
    PARSE_FAILED = "PARSE_FAILED"
    TERMINAL = "TERMINAL"


class TerminalStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    REFUSAL = "REFUSAL"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    NO_OP = "NO_OP"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"


class FakeScenario(str, Enum):
    SUCCESS = "SUCCESS"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    REFUSAL = "REFUSAL"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    NO_OP = "NO_OP"
    TIMEOUT = "TIMEOUT"
    HTTP_5XX = "HTTP_5XX"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    STREAMING_SUCCESS = "STREAMING_SUCCESS"
    STREAMING_PARTIAL_ERROR = "STREAMING_PARTIAL_ERROR"
    PARSER_FAILURE = "PARSER_FAILURE"


CPU_REQUIRED_CHECKS = (
    "active_g1_3_source_bound",
    "exact_schedule",
    "g1_2_full_block_preflight",
    "preflight_blocked_ledger",
    "original_identity",
    "target_only_diff",
    "fake_provider_matrix",
    "retry_policy",
    "append_only_idempotence",
    "blinded_export",
    "blinding_precommit",
    "parser_diagnostics_binding",
    "no_live_paths",
)


@dataclass(frozen=True)
class LoadedReplayCapsule:
    """Deny-by-default runtime projection of one validated G1.3 capsule."""

    publication_manifest_sha256: str
    capsule_file_sha256: str
    capsule_body_sha256: str
    capsule_id: str
    unit_kind: UnitKind
    unit_id: str
    model_id: str
    history_family: str
    semantic_request: JsonValue
    semantic_request_sha256: str
    region_partition: tuple[dict[str, JsonValue], ...]
    non_history_projection_sha256: str
    treatment_surface: dict[str, JsonValue]
    replay_binding: dict[str, JsonValue]
    restore_descriptor: dict[str, JsonValue]
    parser_descriptor: dict[str, JsonValue]
    decoding_configuration: dict[str, JsonValue]
    source_safety: dict[str, JsonValue]

    def public_binding(self) -> dict[str, JsonValue]:
        return {
            "capsule_id": self.capsule_id,
            "unit_kind": self.unit_kind.value,
            "unit_id": self.unit_id,
            "model_id": self.model_id,
            "history_family": self.history_family,
            "publication_manifest_sha256": self.publication_manifest_sha256,
            "capsule_file_sha256": self.capsule_file_sha256,
            "capsule_body_sha256": self.capsule_body_sha256,
            "semantic_request_sha256": self.semantic_request_sha256,
            "non_history_projection_sha256": self.non_history_projection_sha256,
        }


@dataclass(frozen=True)
class ScheduleEntry:
    unit_kind: UnitKind
    unit_id: str
    model_id: str
    block_index: int
    repeat_index: int
    replay_seed: int
    arm_order_index: int
    arm: ArmKind
    block_arm_order: tuple[ArmKind, ...]
    arm_order_input_sha256: str
    block_arm_order_sha256: str
    schedule_id: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "unit_kind": self.unit_kind.value,
            "unit_id": self.unit_id,
            "model_id": self.model_id,
            "block_index": self.block_index,
            "repeat_index": self.repeat_index,
            "replay_seed": self.replay_seed,
            "arm_order_index": self.arm_order_index,
            "arm_id": self.arm.value,
            "block_arm_order": [arm.value for arm in self.block_arm_order],
            "arm_order_contract": (
                "STRICT_FIVE_ARM_ROTATION_V1"
                if self.unit_kind is UnitKind.STRICT_MHR
                else "CLEAN_TWO_ARM_BALANCED_V1"
            ),
            "arm_order_salt": ARM_ORDER_SALT,
            "arm_order_input_sha256": self.arm_order_input_sha256,
            "block_arm_order_sha256": self.block_arm_order_sha256,
            "schedule_id": self.schedule_id,
        }


@dataclass(frozen=True)
class InvocationPlan:
    run_id: str
    execution_domain: ExecutionDomain
    schedule: ScheduleEntry
    capsule_binding: dict[str, JsonValue]
    plan_set_sha256: str
    selected_plan_sha256: str
    history_codec_id: str
    history_codec_contract_version: str
    provider_codec_id: str
    provider_contract_version: str
    endpoint_revision: str
    captured_parser_descriptor_sha256: str
    parser_binding_sha256: str
    model_binding_sha256: str
    provider_binding_sha256: str
    history_codec_sha256: str
    provider_codec_sha256: str
    model_parameters_sha256: str
    code_sha256: str
    config_sha256: str
    live_run_ready_seal_sha256: str | None = None

    def __post_init__(self) -> None:
        binding = self.capsule_binding
        binding_keys = {
            "capsule_id",
            "unit_kind",
            "unit_id",
            "model_id",
            "history_family",
            "publication_manifest_sha256",
            "capsule_file_sha256",
            "capsule_body_sha256",
            "semantic_request_sha256",
            "non_history_projection_sha256",
        }
        hash_fields = (
            self.plan_set_sha256,
            self.selected_plan_sha256,
            self.captured_parser_descriptor_sha256,
            self.parser_binding_sha256,
            self.model_binding_sha256,
            self.provider_binding_sha256,
            self.history_codec_sha256,
            self.provider_codec_sha256,
            self.model_parameters_sha256,
            self.code_sha256,
            self.config_sha256,
        )
        if (
            self.execution_domain is not ExecutionDomain.FAKE_CONFORMANCE
            or not _matches(_RUN_ID_RE, self.run_id)
            or not isinstance(binding, dict)  # type: ignore[redundant-expr]
            or set(binding) != binding_keys
            or not _matches(_CAPSULE_ID_RE, binding.get("capsule_id"))
            or binding.get("unit_kind") not in {item.value for item in UnitKind}
            or not _matches(_UNIT_ID_RE, binding.get("unit_id"))
            or binding.get("model_id") not in {"qwen3vl_8b", "mai_ui_8b"}
            or binding.get("history_family") not in {"flat_progress", "raw_replay"}
            or any(
                not _is_sha256(binding.get(key))
                for key in (
                    "publication_manifest_sha256",
                    "capsule_file_sha256",
                    "capsule_body_sha256",
                    "semantic_request_sha256",
                    "non_history_projection_sha256",
                )
            )
            or not isinstance(self.schedule.unit_kind, UnitKind)  # type: ignore[redundant-expr]
            or not isinstance(self.schedule.arm, ArmKind)  # type: ignore[redundant-expr]
            or type(self.schedule.block_index) is not int
            or not 1 <= self.schedule.block_index <= 6
            or type(self.schedule.repeat_index) is not int
            or self.schedule.repeat_index not in REPEATS
            or type(self.schedule.replay_seed) is not int
            or self.schedule.replay_seed not in REPLAY_SEEDS
            or type(self.schedule.arm_order_index) is not int
            or not 0 <= self.schedule.arm_order_index <= 4
            or not isinstance(  # type: ignore[redundant-expr]
                self.schedule.block_arm_order, tuple
            )
            or not 2 <= len(self.schedule.block_arm_order) <= 5
            or any(not isinstance(arm, ArmKind) for arm in self.schedule.block_arm_order)
            or len(set(self.schedule.block_arm_order)) != len(self.schedule.block_arm_order)
            or binding.get("unit_kind") != self.schedule.unit_kind.value
            or binding.get("unit_id") != self.schedule.unit_id
            or binding.get("model_id") != self.schedule.model_id
            or not _matches(_SCHEDULE_ID_RE, self.schedule.schedule_id)
            or not _is_sha256(self.schedule.arm_order_input_sha256)
            or not _is_sha256(self.schedule.block_arm_order_sha256)
            or not isinstance(self.history_codec_id, str)  # type: ignore[redundant-expr]
            or not self.history_codec_id
            or not isinstance(  # type: ignore[redundant-expr]
                self.history_codec_contract_version, str
            )
            or not self.history_codec_contract_version
            or self.provider_codec_id != "mobileworld.g1.provider.fake-conformance/v1"
            or self.provider_contract_version != "v1"
            or self.endpoint_revision != "fake://network-forbidden/v1"
            or any(not _is_sha256(value) for value in hash_fields)
            or self.live_run_ready_seal_sha256 is not None
        ):
            raise ReplayRunnerError(
                "INVOCATION_PLAN_INVALID",
                "invocation plan does not satisfy the closed CPU-only schema contract",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": INVOCATION_PLAN_SCHEMA_VERSION,
            "record_type": "g1_replay_invocation_plan",
            "protocol_version": PROTOCOL_VERSION,
            "runner_contract_version": RUNNER_CONTRACT_VERSION,
            "run_id": self.run_id,
            "execution_domain": self.execution_domain.value,
            "schedule": self.schedule.to_dict(),
            "capsule_binding": copy_json(self.capsule_binding),
            "plan_set_sha256": self.plan_set_sha256,
            "selected_plan_sha256": self.selected_plan_sha256,
            "history_codec_id": self.history_codec_id,
            "history_codec_contract_version": self.history_codec_contract_version,
            "provider_codec_id": self.provider_codec_id,
            "provider_contract_version": self.provider_contract_version,
            "endpoint_revision": self.endpoint_revision,
            "captured_parser_descriptor_sha256": self.captured_parser_descriptor_sha256,
            "parser_binding_sha256": self.parser_binding_sha256,
            "model_binding_sha256": self.model_binding_sha256,
            "provider_binding_sha256": self.provider_binding_sha256,
            "history_codec_sha256": self.history_codec_sha256,
            "provider_codec_sha256": self.provider_codec_sha256,
            "model_parameters_sha256": self.model_parameters_sha256,
            "code_sha256": self.code_sha256,
            "config_sha256": self.config_sha256,
            "live_run_ready_seal_sha256": self.live_run_ready_seal_sha256,
            "provider_invocation_allowed": False,
            "treatment_response_generation_allowed": False,
        }


@dataclass(frozen=True)
class InvarianceReport:
    report_id: str
    valid: bool
    source_request_sha256: str
    rendered_request_sha256: str
    final_application_request_sha256: str
    encoded_request_sha256: str | None
    non_history_projection_sha256: str
    history_projection_sha256: str
    render_result_sha256: str
    validation_receipt_sha256: str
    target_diff_sha256: str
    requested_arm: ArmKind
    target_only_diff: bool
    original_semantic_identity: bool
    caller_input_immutable: bool
    source_mapping_reversible: bool
    roles_and_order_preserved: bool
    tools_preserved: bool
    current_observation_preserved: bool
    model_and_sampling_preserved: bool
    binary_artifacts_preserved: bool
    checks: tuple[str, ...]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": INVARIANCE_SCHEMA_VERSION,
            "record_type": "g1_replay_invariance_report",
            "protocol_version": PROTOCOL_VERSION,
            "report_id": self.report_id,
            "valid": self.valid,
            "provider_invocation_allowed": False,
            "source_request_sha256": self.source_request_sha256,
            "rendered_request_sha256": self.rendered_request_sha256,
            "final_application_request_sha256": self.final_application_request_sha256,
            "encoded_request_sha256": self.encoded_request_sha256,
            "non_history_projection_sha256": self.non_history_projection_sha256,
            "history_projection_sha256": self.history_projection_sha256,
            "render_result_sha256": self.render_result_sha256,
            "validation_receipt_sha256": self.validation_receipt_sha256,
            "target_diff_sha256": self.target_diff_sha256,
            "requested_arm": self.requested_arm.value,
            "target_only_diff": self.target_only_diff,
            "original_semantic_identity": self.original_semantic_identity,
            "caller_input_immutable": self.caller_input_immutable,
            "source_mapping_reversible": self.source_mapping_reversible,
            "roles_and_order_preserved": self.roles_and_order_preserved,
            "tools_preserved": self.tools_preserved,
            "current_observation_preserved": self.current_observation_preserved,
            "model_and_sampling_preserved": self.model_and_sampling_preserved,
            "binary_artifacts_preserved": self.binary_artifacts_preserved,
            "checks": list(self.checks),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ChunkRecord:
    chunk_index: int
    byte_count: int
    sha256: str
    is_final: bool
    content_ref: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "chunk_index": self.chunk_index,
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "is_final": self.is_final,
            "content_ref": copy_json(self.content_ref),
        }


@dataclass(frozen=True)
class ProviderExchange:
    exchange_id: str
    run_id: str
    provider_attempt_index: int
    provider_codec_id: str
    provider_contract_version: str
    endpoint_revision: str
    application_request_sha256: str
    final_application_request_sha256: str
    encoded_request_sha256: str
    model_parameters_sha256: str
    request_byte_count: int
    encoded_request_ref: dict[str, JsonValue]
    response_sha256: str | None
    response_byte_count: int | None
    raw_response_ref: dict[str, JsonValue] | None
    chunks: tuple[ChunkRecord, ...]
    latency_ms: int | None
    token_usage: dict[str, JsonValue] | None
    transport_status: str
    error_code: str | None
    retryable: bool
    simulated: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PROVIDER_EXCHANGE_SCHEMA_VERSION,
            "record_type": "g1_replay_provider_exchange",
            "protocol_version": PROTOCOL_VERSION,
            "exchange_id": self.exchange_id,
            "run_id": self.run_id,
            "provider_attempt_index": self.provider_attempt_index,
            "provider_codec_id": self.provider_codec_id,
            "provider_contract_version": self.provider_contract_version,
            "endpoint_revision": self.endpoint_revision,
            "application_request_sha256": self.application_request_sha256,
            "final_application_request_sha256": self.final_application_request_sha256,
            "encoded_request_sha256": self.encoded_request_sha256,
            "model_parameters_sha256": self.model_parameters_sha256,
            "request_byte_count": self.request_byte_count,
            "encoded_request_ref": copy_json(self.encoded_request_ref),
            "response_sha256": self.response_sha256,
            "response_byte_count": self.response_byte_count,
            "raw_response_ref": copy_json(self.raw_response_ref),
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "latency_ms": self.latency_ms,
            "token_usage": copy_json(self.token_usage),
            "transport_status": self.transport_status,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "simulated": self.simulated,
            "external_provider_invoked": False,
            "gpu_used": False,
        }


@dataclass(frozen=True)
class AttemptEvent:
    event_id: str
    run_id: str
    seq: int
    previous_event_sha256: str | None
    event_kind: AttemptEventKind
    provider_attempt_index: int | None
    payload: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": ATTEMPT_EVENT_SCHEMA_VERSION,
            "record_type": "g1_replay_attempt_event",
            "protocol_version": PROTOCOL_VERSION,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "previous_event_sha256": self.previous_event_sha256,
            "event_kind": self.event_kind.value,
            "provider_attempt_index": self.provider_attempt_index,
            "payload": copy_json(self.payload),
            "raw_collector_event": False,
            "generated_action_executed": False,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


@dataclass(frozen=True)
class TerminalAttemptRecord:
    run_id: str
    status: TerminalStatus
    provider_attempt_count: int
    final_event_sha256: str
    provider_result: ProviderResult | None
    parser_diagnostics: dict[str, JsonValue]
    retry_reason: str | None
    idempotent_reuse: bool = False

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": TERMINAL_ATTEMPT_SCHEMA_VERSION,
            "record_type": "g1_replay_terminal_attempt",
            "protocol_version": PROTOCOL_VERSION,
            "run_id": self.run_id,
            "status": self.status.value,
            "provider_attempt_count": self.provider_attempt_count,
            "final_event_sha256": self.final_event_sha256,
            "provider_result": (
                None if self.provider_result is None else self.provider_result.to_dict()
            ),
            "parser_diagnostics": copy_json(self.parser_diagnostics),
            "retry_reason": self.retry_reason,
            "idempotent_reuse": self.idempotent_reuse,
            "generated_action_executed": False,
            "response_fed_to_later_request": False,
            "scientific_count_eligible": False,
        }


@dataclass(frozen=True)
class BlindingMappingRecord:
    blinded_packet_id: str
    run_id: str
    arm: ArmKind
    schedule_id: str
    blinding_nonce: str
    key_commitment_sha256: str
    forbidden_value_set_sha256: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": BLINDING_MAPPING_SCHEMA_VERSION,
            "record_type": "g1_confidential_blinding_mapping",
            "blinded_packet_id": self.blinded_packet_id,
            "run_id": self.run_id,
            "arm_id": self.arm.value,
            "schedule_id": self.schedule_id,
            "blinding_nonce": self.blinding_nonce,
            "key_commitment_sha256": self.key_commitment_sha256,
            "forbidden_value_set_sha256": self.forbidden_value_set_sha256,
            "mapping_sealed": True,
            "scorer_visible": False,
        }


@dataclass(frozen=True)
class BlindedActionPacket:
    blinded_packet_id: str
    _normalized_action_json: bytes = field(repr=False)
    parser_outcome: str
    _parser_diagnostics_json: bytes = field(repr=False)
    _confidential_values: tuple[str, ...] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            action = json.loads(self._normalized_action_json)
            diagnostics = json.loads(self._parser_diagnostics_json)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayRunnerError(
                "BLINDED_PACKET_INVALID", "blinded packet snapshots are not canonical JSON"
            ) from exc
        if (
            canonical_json_bytes(cast(JsonValue, action)) != self._normalized_action_json
            or canonical_json_bytes(cast(JsonValue, diagnostics)) != self._parser_diagnostics_json
            or (action is not None and not isinstance(action, dict))
            or not isinstance(diagnostics, dict)
            or not self._confidential_values
        ):
            raise ReplayRunnerError(
                "BLINDED_PACKET_INVALID", "blinded packet snapshots are invalid"
            )

    @property
    def normalized_action(self) -> dict[str, JsonValue] | None:
        value = json.loads(self._normalized_action_json)
        return None if value is None else cast(dict[str, JsonValue], value)

    @property
    def parser_diagnostics(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], json.loads(self._parser_diagnostics_json))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": BLINDED_PACKET_SCHEMA_VERSION,
            "record_type": "g1_blinded_action_packet",
            "blinded_packet_id": self.blinded_packet_id,
            "normalized_action": copy_json(self.normalized_action),
            "parser_outcome": self.parser_outcome,
            "parser_diagnostics": copy_json(self.parser_diagnostics),
            "treatment_identity_present": False,
        }


@dataclass(frozen=True)
class CpuReadinessManifest:
    code_sha256: str
    schema_set_sha256: str
    fake_scenarios: tuple[str, ...]
    focused_test_count: int
    checks: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            not _is_sha256(self.code_sha256)
            or not _is_sha256(self.schema_set_sha256)
            or type(self.focused_test_count) is not int
            or self.focused_test_count < 1
        ):
            raise ReplayRunnerError(
                "CPU_READINESS_INVALID",
                "CPU readiness hashes and focused-test count are invalid",
            )
        if self.fake_scenarios != tuple(item.value for item in FakeScenario):
            raise ReplayRunnerError(
                "CPU_READINESS_INCOMPLETE",
                "CPU readiness requires the exact frozen fake scenario catalog",
            )
        if self.checks != CPU_REQUIRED_CHECKS:
            raise ReplayRunnerError(
                "CPU_READINESS_INCOMPLETE",
                "CPU readiness requires the exact mandatory conformance checks",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": CPU_MANIFEST_SCHEMA_VERSION,
            "record_type": "g1_replay_runner_cpu_manifest",
            "protocol_version": PROTOCOL_VERSION,
            "runner_contract_version": RUNNER_CONTRACT_VERSION,
            "runner_version": RUNNER_VERSION,
            "story": "G1.4_CPU_ONLY_PARTIAL",
            "code_sha256": self.code_sha256,
            "schema_set_sha256": self.schema_set_sha256,
            "fake_scenarios": list(self.fake_scenarios),
            "focused_test_count": self.focused_test_count,
            "checks": list(self.checks),
            "readiness": {
                "cpu_contract_implemented": True,
                "fake_provider_conformance_ready": True,
                "live_transport_validation_complete": False,
                "live_history_codec_ready": False,
                "curated_transformations_ready": False,
                "run_ready_seal_present": False,
                "provider_invocation_allowed": False,
                "treatment_response_generation_allowed": False,
                "formal_replay_ready": False,
            },
            "safety": {
                "external_provider_invoked": False,
                "gpu_used": False,
                "gui_action_executed": False,
                "generated_action_executed": False,
                "raw_collector_mutated": False,
                "automatic_semantic_inference_performed": False,
                "runtime_sentinel_enabled": False,
            },
        }
