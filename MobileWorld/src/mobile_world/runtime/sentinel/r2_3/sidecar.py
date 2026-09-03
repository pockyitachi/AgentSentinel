"""Hash-only sidecar receipts for the R2.3 rubric boundary."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol, runtime_checkable

from mobile_world.offline.causal_replay.contracts import JsonValue

RUBRIC_RECEIPT_SCHEMA_VERSION = "mobileworld.runtime.rubric-receipt/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")


class RubricReceiptOperation(StrEnum):
    TASK_START_GENERATE = "TASK_START_GENERATE"
    EXPLICIT_REVISION = "EXPLICIT_REVISION"
    TRACK = "TRACK"
    LINK_RELEVANCE = "LINK_RELEVANCE"
    COMPARE_TOPOLOGY = "COMPARE_TOPOLOGY"


class RubricEvaluationStatus(StrEnum):
    ADMITTED = "ADMITTED"
    INPUT_REJECTED = "INPUT_REJECTED"
    BACKEND_ERROR = "BACKEND_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    ADMISSION_REJECTED = "ADMISSION_REJECTED"
    STATE_CONFLICT = "STATE_CONFLICT"
    SIDECAR_FAILURE = "SIDECAR_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def _require_id(value: object, name: str) -> None:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe bounded ID")


def _require_sha(value: object, name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class RubricReceiptV1:
    receipt_id: str
    task_run_id: str
    logical_call_id: str | None
    operation: RubricReceiptOperation
    topology_kind: str
    status: RubricEvaluationStatus
    fallback_code: str | None
    backend_id: str
    backend_version: str
    prompt_sha256: str
    input_schema_sha256: str | None
    output_schema_sha256: str
    config_sha256: str
    input_sha256: str
    raw_backend_output_sha256: str | None
    parsed_output_sha256: str | None
    admitted_output_sha256: str | None
    rubric_id: str | None
    rubric_version: int | None
    rubric_sha256: str | None
    prior_state_sha256: str | None
    final_state_sha256: str | None
    backend_calls: int
    task_start_generation_calls: int
    explicit_revision_calls: int
    runtime_tracking_calls: int
    relevance_link_calls: int
    packet_build_latency_ns: int
    backend_latency_ns: int
    admission_latency_ns: int
    state_update_latency_ns: int
    total_latency_ns: int
    pending_count: int = 0
    in_progress_count: int = 0
    satisfied_count: int = 0
    violated_count: int = 0
    unknown_milestone_count: int = 0
    viable_path_count: int = 0
    inactive_path_count: int = 0
    unknown_path_count: int = 0
    frontier_count: int = 0
    active_path_relevance_count: int = 0
    inactive_branch_relevance_count: int = 0
    path_independent_relevance_count: int = 0
    unknown_relevance_count: int = 0
    archive_shadow_count: int = 0
    unknown_or_abstain_count: int = 0
    validation_checks: tuple[str, ...] = ()
    schema_version: str = RUBRIC_RECEIPT_SCHEMA_VERSION
    execution_scope: str = "SHADOW_ONLY"
    backend_kind: str = "INJECTED_FAKE"
    transport_authority: str = "CPU_OFFLINE_FAKE"
    external_network_attempted: bool = False
    model_call_attempted: bool = False
    local_gpu_used: bool = False
    mobileworld_action_executed: bool = False
    actor_request_mutated: bool = False
    collector_raw_mutated: bool = False
    task_text_persisted: bool = False
    screenshot_persisted: bool = False
    backend_output_persisted: bool = False
    reasoning_persisted: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != RUBRIC_RECEIPT_SCHEMA_VERSION
        ):
            raise ValueError("unknown rubric receipt schema version")
        _require_id(self.receipt_id, "receipt_id")
        _require_id(self.task_run_id, "task_run_id")
        if self.logical_call_id is not None:
            _require_id(self.logical_call_id, "logical_call_id")
        if type(self.operation) is not RubricReceiptOperation:
            raise TypeError("operation must be an exact RubricReceiptOperation")
        if type(self.status) is not RubricEvaluationStatus:
            raise TypeError("status must be an exact RubricEvaluationStatus")
        if type(self.execution_scope) is not str or self.execution_scope != "SHADOW_ONLY":
            raise ValueError("execution_scope is outside the R2.3 SHADOW-only scope")
        for value, name in (
            (self.backend_kind, "backend_kind"),
            (self.transport_authority, "transport_authority"),
        ):
            if type(value) is not str:
                raise ValueError(f"{name} must be exact text")
        if self.topology_kind not in {"ISOLATED_HISTORY_FREE", "JOINT_NON_INDEPENDENT"}:
            raise ValueError("unknown rubric topology")
        for name in ("backend_id", "backend_version"):
            _require_id(getattr(self, name), name)
        for name in (
            "prompt_sha256",
            "output_schema_sha256",
            "config_sha256",
            "input_sha256",
        ):
            _require_sha(getattr(self, name), name)
        _require_sha(self.input_schema_sha256, "input_schema_sha256", nullable=True)
        if self.operation is RubricReceiptOperation.TRACK:
            if self.input_schema_sha256 is None:
                raise ValueError("TRACK receipt requires its tracking-packet input schema")
        elif self.input_schema_sha256 is not None:
            raise ValueError("only TRACK has a checked-in input schema in R2.3")
        for name in (
            "raw_backend_output_sha256",
            "parsed_output_sha256",
            "admitted_output_sha256",
            "rubric_sha256",
            "prior_state_sha256",
            "final_state_sha256",
        ):
            _require_sha(getattr(self, name), name, nullable=True)
        if self.rubric_id is not None:
            _require_id(self.rubric_id, "rubric_id")
        if self.rubric_version is not None and (
            type(self.rubric_version) is not int or self.rubric_version < 1
        ):
            raise ValueError("rubric_version must be null or positive")
        if self.status is RubricEvaluationStatus.ADMITTED:
            if self.fallback_code is not None or self.admitted_output_sha256 is None:
                raise ValueError("ADMITTED receipts need output and no fallback")
        else:
            _require_id(self.fallback_code, "fallback_code")
            if self.admitted_output_sha256 is not None:
                raise ValueError("non-ADMITTED receipts cannot bind admitted output")
        if self.parsed_output_sha256 is not None and self.raw_backend_output_sha256 is None:
            raise ValueError("parsed output requires a raw backend-output binding")
        if self.raw_backend_output_sha256 is not None and self.backend_calls != 1:
            raise ValueError("raw backend output requires exactly one backend call")
        rubric_identity = (self.rubric_id, self.rubric_version, self.rubric_sha256)
        if any(value is not None for value in rubric_identity) and not all(
            value is not None for value in rubric_identity
        ):
            raise ValueError("rubric identity fields must be all present or all absent")
        for name in (
            "backend_calls",
            "task_start_generation_calls",
            "explicit_revision_calls",
            "runtime_tracking_calls",
            "relevance_link_calls",
            "packet_build_latency_ns",
            "backend_latency_ns",
            "admission_latency_ns",
            "state_update_latency_ns",
            "total_latency_ns",
            "pending_count",
            "in_progress_count",
            "satisfied_count",
            "violated_count",
            "unknown_milestone_count",
            "viable_path_count",
            "inactive_path_count",
            "unknown_path_count",
            "frontier_count",
            "active_path_relevance_count",
            "inactive_branch_relevance_count",
            "path_independent_relevance_count",
            "unknown_relevance_count",
            "archive_shadow_count",
            "unknown_or_abstain_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative exact integer")
        if self.backend_calls not in {0, 1}:
            raise ValueError("backend_calls must be zero or one")
        if type(self.validation_checks) is not tuple or any(
            type(item) is not str or _SAFE_ID.fullmatch(item) is None
            for item in self.validation_checks
        ):
            raise ValueError("validation_checks must contain safe IDs")
        if len(self.validation_checks) > 128:
            raise ValueError("validation_checks exceeds the receipt schema bound")
        if len(self.validation_checks) != len(set(self.validation_checks)):
            raise ValueError("validation_checks must be unique")
        for name in (
            "mobileworld_action_executed",
            "actor_request_mutated",
            "collector_raw_mutated",
            "task_text_persisted",
            "screenshot_persisted",
            "backend_output_persisted",
            "reasoning_persisted",
        ):
            value = getattr(self, name)
            if type(value) is not bool or value:
                raise ValueError(f"{name} must be exact false")
        for name in (
            "external_network_attempted",
            "model_call_attempted",
            "local_gpu_used",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be an exact bool")
        fake = (
            self.backend_kind == "INJECTED_FAKE"
            and self.transport_authority == "CPU_OFFLINE_FAKE"
            and not self.external_network_attempted
            and not self.model_call_attempted
            and not self.local_gpu_used
        )
        owner_authorized_live = (
            self.backend_kind == "OPENAI_RESPONSES"
            and self.transport_authority == "EXPLICIT_OWNER_AUTHORIZATION"
            and self.external_network_attempted
            and self.model_call_attempted
            and not self.local_gpu_used
        )
        if not (fake or owner_authorized_live):
            raise ValueError("backend resource flags differ from transport provenance")
        if self.total_latency_ns < max(
            self.packet_build_latency_ns,
            self.backend_latency_ns,
            self.admission_latency_ns,
            self.state_update_latency_ns,
        ):
            raise ValueError("total latency cannot be below a stage latency")

    @property
    def sha256(self) -> str:
        return rubric_receipt_sha256(self)


def rubric_receipt_projection(receipt: RubricReceiptV1) -> dict[str, JsonValue]:
    if type(receipt) is not RubricReceiptV1:
        raise TypeError("receipt must be an exact RubricReceiptV1")
    return {
        field_name: (
            value.value
            if type(value) in {RubricReceiptOperation, RubricEvaluationStatus}
            else list(value)
            if type(value) is tuple
            else value
        )
        for field_name in receipt.__dataclass_fields__
        if (value := getattr(receipt, field_name)) is not None
    }


def rubric_receipt_sha256(receipt: RubricReceiptV1) -> str:
    payload = json.dumps(
        rubric_receipt_projection(receipt),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@runtime_checkable
class RubricReceiptSinkV1(Protocol):
    def emit(self, receipt: RubricReceiptV1) -> None: ...


class MemoryRubricReceiptSinkV1:
    """In-memory CPU test sink; it never stores graph, GUI, or backend content."""

    def __init__(self) -> None:
        self._receipts: list[RubricReceiptV1] = []
        self._lock = Lock()

    def emit(self, receipt: RubricReceiptV1) -> None:
        if type(receipt) is not RubricReceiptV1:
            raise TypeError("receipt must be an exact RubricReceiptV1")
        with self._lock:
            self._receipts.append(deepcopy(receipt))

    @property
    def receipts(self) -> tuple[RubricReceiptV1, ...]:
        with self._lock:
            return tuple(deepcopy(self._receipts))


__all__ = [
    "MemoryRubricReceiptSinkV1",
    "RUBRIC_RECEIPT_SCHEMA_VERSION",
    "RubricEvaluationStatus",
    "RubricReceiptOperation",
    "RubricReceiptSinkV1",
    "RubricReceiptV1",
    "rubric_receipt_projection",
    "rubric_receipt_sha256",
]
