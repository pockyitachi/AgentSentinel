"""Closed contracts for the R2.4 runtime vertical slice.

R2.4 is an additive overlay.  It does not relax the accepted R2.2 SHADOW
contract and it never turns an R2.2 value into evidence of a live deployment.
Execution is always bound to a separate authority hash.  CPU tests use the
module-owned offline fake-provider authority; a live adapter must bind an
owner-authored run-manifest hash and is admitted separately by the common seam.

All hashes are built from module-owned projections.  User-overridable
serializers are deliberately outside the trust boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonValue
from mobile_world.runtime.sentinel.contracts import (
    SentinelContext,
    SentinelDecisionKind,
    SentinelReceipt,
    SentinelResult,
)
from mobile_world.runtime.sentinel.r2_2.contracts import (
    PolicyExecutionControlV1,
    RuntimeOperationKind,
)

RUNTIME_VERTICAL_PLAN_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-admitted-plan/v1"
RUNTIME_VERTICAL_OUTPUT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-policy-output/v1"
RUNTIME_VERTICAL_RECEIPT_BRIDGE_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-receipt-bridge/v1"
)
RUNTIME_VERTICAL_SENTINEL_RESULT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-result/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CHECK_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_MAX_GRAPH_DEPTH = 64
_MAX_GRAPH_VISITS = 262_144


class R24ContractError(ValueError):
    """A bounded, machine-readable R2.4 validation failure."""

    def __init__(self, code: str, message: str) -> None:
        if type(code) is not str or _CHECK_CODE.fullmatch(code) is None:
            raise ValueError("R24ContractError requires a closed check code")
        self.code = code
        super().__init__(f"{code}: {message}")


class RuntimeVerticalExecutionScope(StrEnum):
    """Execution authority carried by the additive R2.4 overlay."""

    CPU_FAKE_ACTIVE = "CPU_FAKE_ACTIVE"
    OWNER_AUTHORIZED_LIVE_ACTIVE = "OWNER_AUTHORIZED_LIVE_ACTIVE"


class RuntimeVerticalStatus(StrEnum):
    EVALUATED = "EVALUATED"
    NO_ELIGIBLE_HISTORY = "NO_ELIGIBLE_HISTORY"


class RuntimeVerticalBridgeStatus(StrEnum):
    NO_HISTORY_AVAILABLE = "NO_HISTORY_AVAILABLE"
    NO_ELIGIBLE_HISTORY = "NO_ELIGIBLE_HISTORY"
    EVALUATED = "EVALUATED"


class RuntimeReplacementTemplate(StrEnum):
    """Closed Sentinel-authored text; no backend text crosses this boundary."""

    REFUTED_HISTORY_FACT_V1 = "REFUTED_HISTORY_FACT_V1"
    STALE_AFTER_INVALIDATION_V1 = "STALE_AFTER_INVALIDATION_V1"


_REPLACEMENT_TEXT: dict[RuntimeReplacementTemplate, str] = {
    RuntimeReplacementTemplate.REFUTED_HISTORY_FACT_V1: (
        "[Sentinel correction: this prior history claim was refuted by trusted runtime evidence.]"
    ),
    RuntimeReplacementTemplate.STALE_AFTER_INVALIDATION_V1: (
        "[Sentinel correction: this prior history claim became stale after later trusted runtime "
        "evidence.]"
    ),
}


def replacement_text_for_template(template: RuntimeReplacementTemplate) -> str:
    if type(template) is not RuntimeReplacementTemplate:
        raise R24ContractError(
            "UNTRUSTED_RUNTIME_TYPE", "replacement template must use the exact enum"
        )
    return _REPLACEMENT_TEXT[template]


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise R24ContractError("INVALID_SHA256", f"{name} must be lowercase SHA-256")
    return value


def _require_runtime_id(value: object, name: str) -> str:
    if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
        raise R24ContractError("INVALID_RUNTIME_ID", f"{name} is not a bounded runtime ID")
    return value


def _require_semantic_id(value: object, name: str) -> str:
    if type(value) is not str or _SEMANTIC_ID.fullmatch(value) is None:
        raise R24ContractError("INVALID_SEMANTIC_ID", f"{name} is not a bounded semantic ID")
    return value


def _require_checks(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise R24ContractError("VALIDATION_CHECKS_MISSING", "validation checks are required")
    checks = cast(tuple[object, ...], value)
    if any(type(item) is not str or _CHECK_CODE.fullmatch(item) is None for item in checks):
        raise R24ContractError("INVALID_VALIDATION_CHECK", "validation check is not closed")
    projected = cast(tuple[str, ...], checks)
    if len(projected) != len(set(projected)):
        raise R24ContractError("DUPLICATE_VALIDATION_CHECK", "validation checks repeat")
    return projected


def _require_exact_tuple(
    value: object,
    member_type: type[object],
    name: str,
    *,
    maximum: int = 256,
) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", f"{name} must be an exact tuple")
    items = cast(tuple[object, ...], value)
    if len(items) > maximum:
        raise R24ContractError("RUNTIME_COLLECTION_TOO_LARGE", f"{name} exceeds {maximum}")
    if any(type(item) is not member_type for item in items):
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", f"{name} has an untrusted member")
    return items


def _canonical_json_bytes(value: JsonValue) -> bytes:
    """Canonicalize only after an iterative exact-tree validation."""

    _validate_json_graph(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise R24ContractError("NON_CANONICAL_JSON", "value is outside canonical JSON") from exc


def _validate_json_graph(value: object) -> None:
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    visits = 0
    while stack:
        item, depth, exiting = stack.pop()
        if exiting:
            active.remove(id(item))
            continue
        visits += 1
        if visits > _MAX_GRAPH_VISITS:
            raise R24ContractError("GRAPH_NODE_LIMIT", "JSON graph visit budget exceeded")
        if depth > _MAX_GRAPH_DEPTH:
            raise R24ContractError("GRAPH_DEPTH_LIMIT", "JSON graph depth exceeded")
        if item is None or type(item) in {bool, int, str}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise R24ContractError("NON_CANONICAL_JSON", "JSON number is not finite")
            continue
        if type(item) not in {list, dict}:
            raise R24ContractError("NON_CANONICAL_JSON", "JSON value has an untrusted type")
        identity = id(item)
        if identity in active:
            raise R24ContractError("GRAPH_CYCLE", "JSON graph contains a cycle")
        active.add(identity)
        stack.append((item, depth, True))
        if type(item) is list:
            children = cast(list[object], item)
        else:
            mapping = cast(dict[object, object], item)
            if any(type(key) is not str for key in mapping):
                raise R24ContractError("NON_CANONICAL_JSON", "JSON object key is not text")
            children = list(mapping.values())
        for child in reversed(children):
            stack.append((child, depth + 1, False))


def canonical_sha256(value: JsonValue) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def canonical_json_bytes(value: JsonValue) -> bytes:
    return _canonical_json_bytes(value)


def snapshot_json_value(value: JsonValue) -> JsonValue:
    """Return a detached canonical JSON tree after bounded iterative validation."""

    payload = _canonical_json_bytes(value)
    decoded = json.loads(payload)
    _validate_json_graph(decoded)
    return cast(JsonValue, decoded)


@dataclass(frozen=True, slots=True)
class CpuFakeActiveAuthorityV1:
    """An explicit non-live authority token issued only for CPU fake execution."""

    offline: bool = True
    fake_provider: bool = True
    network_allowed: bool = False
    gpu_allowed: bool = False
    actor_actions_allowed: bool = False
    scope: RuntimeVerticalExecutionScope = RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE

    def __post_init__(self) -> None:
        if type(self.scope) is not RuntimeVerticalExecutionScope:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "authority scope is untrusted")
        expected = (
            (self.offline, True),
            (self.fake_provider, True),
            (self.network_allowed, False),
            (self.gpu_allowed, False),
            (self.actor_actions_allowed, False),
        )
        if any(
            type(cast(object, value)) is not bool or value is not required
            for value, required in expected
        ):
            raise R24ContractError(
                "CPU_FAKE_AUTHORITY_REQUIRED", "authority exceeds the offline fake boundary"
            )


def issue_cpu_fake_active_authority() -> CpuFakeActiveAuthorityV1:
    """Make the explicit token required by the CPU-only policy adapter."""

    return CpuFakeActiveAuthorityV1()


def cpu_fake_active_authority_projection(
    value: CpuFakeActiveAuthorityV1,
) -> dict[str, JsonValue]:
    """Return the exact non-live authority projection used by plans/receipts."""

    if type(value) is not CpuFakeActiveAuthorityV1:
        raise R24ContractError(
            "CPU_FAKE_AUTHORITY_REQUIRED", "authority must use the exact CPU fake type"
        )
    trusted = CpuFakeActiveAuthorityV1(
        offline=value.offline,
        fake_provider=value.fake_provider,
        network_allowed=value.network_allowed,
        gpu_allowed=value.gpu_allowed,
        actor_actions_allowed=value.actor_actions_allowed,
        scope=value.scope,
    )
    return {
        "scope": trusted.scope.value,
        "offline": trusted.offline,
        "fake_provider": trusted.fake_provider,
        "network_allowed": trusted.network_allowed,
        "gpu_allowed": trusted.gpu_allowed,
        "actor_actions_allowed": trusted.actor_actions_allowed,
    }


def cpu_fake_active_authority_sha256(value: CpuFakeActiveAuthorityV1) -> str:
    return canonical_sha256(cast(JsonValue, cpu_fake_active_authority_projection(value)))


CPU_FAKE_ACTIVE_AUTHORITY_SHA256 = cpu_fake_active_authority_sha256(CpuFakeActiveAuthorityV1())


@dataclass(frozen=True, slots=True)
class RuntimeVerticalDecisionV1:
    decision_id: str
    target_id: str
    operation: RuntimeOperationKind
    source_decision_sha256: str

    def __post_init__(self) -> None:
        _require_semantic_id(self.decision_id, "decision_id")
        _require_semantic_id(self.target_id, "target_id")
        if type(self.operation) is not RuntimeOperationKind:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "operation enum is untrusted")
        _require_sha256(self.source_decision_sha256, "source_decision_sha256")


@dataclass(frozen=True, slots=True)
class RuntimeVerticalOperationV1:
    operation_id: str
    decision_id: str
    target_id: str
    target_record_id: str
    target_span_sha256: str
    kind: RuntimeOperationKind
    source_operation_sha256: str
    replacement_template: RuntimeReplacementTemplate | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.operation_id, "operation_id"),
            (self.decision_id, "decision_id"),
            (self.target_id, "target_id"),
            (self.target_record_id, "target_record_id"),
        ):
            _require_semantic_id(value, name)
        _require_sha256(self.target_span_sha256, "target_span_sha256")
        _require_sha256(self.source_operation_sha256, "source_operation_sha256")
        if type(self.kind) is not RuntimeOperationKind or self.kind not in {
            RuntimeOperationKind.DROP,
            RuntimeOperationKind.REPLACE,
        }:
            raise R24ContractError(
                "NON_MATERIAL_ADMITTED_OPERATION", "vertical plan permits DROP/REPLACE only"
            )
        if self.kind is RuntimeOperationKind.REPLACE:
            if type(self.replacement_template) is not RuntimeReplacementTemplate:
                raise R24ContractError(
                    "FIXED_REPLACEMENT_TEMPLATE_REQUIRED",
                    "REPLACE must select the closed Sentinel template",
                )
        elif self.replacement_template is not None:
            raise R24ContractError(
                "UNEXPECTED_REPLACEMENT_TEMPLATE", "DROP cannot carry replacement data"
            )

    @property
    def replacement_text(self) -> str | None:
        if self.replacement_template is None:
            return None
        return replacement_text_for_template(self.replacement_template)


@dataclass(frozen=True, slots=True)
class RuntimeVerticalAdmittedPlanV1:
    plan_id: str
    logical_call_id: str
    host_id: str
    history_family: str
    history_codec_id: str
    history_codec_contract_version: str
    source_request_sha256: str
    source_policy_output_sha256: str
    source_policy_receipt_sha256: str
    source_transport_descriptor_sha256: str
    source_r22_admitted_plan_sha256: str
    operations: tuple[RuntimeVerticalOperationV1, ...]
    source_transport_binding_sha256: str | None = None
    execution_authority_sha256: str = CPU_FAKE_ACTIVE_AUTHORITY_SHA256
    execution_scope: RuntimeVerticalExecutionScope = RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE
    schema_version: str = RUNTIME_VERTICAL_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != (
            RUNTIME_VERTICAL_PLAN_SCHEMA_VERSION
        ):
            raise R24ContractError("UNKNOWN_SCHEMA_VERSION", "unknown vertical plan schema")
        _require_semantic_id(self.plan_id, "plan_id")
        _require_runtime_id(self.logical_call_id, "logical_call_id")
        _require_runtime_id(self.host_id, "host_id")
        if self.source_transport_binding_sha256 is None:
            object.__setattr__(
                self,
                "source_transport_binding_sha256",
                self.source_transport_descriptor_sha256,
            )
        for semantic_value, name in (
            (self.history_family, "history_family"),
            (self.history_codec_id, "history_codec_id"),
            (self.history_codec_contract_version, "history_codec_contract_version"),
        ):
            _require_semantic_id(semantic_value, name)
        for digest_value, name in (
            (self.source_request_sha256, "source_request_sha256"),
            (self.source_policy_output_sha256, "source_policy_output_sha256"),
            (self.source_policy_receipt_sha256, "source_policy_receipt_sha256"),
            (
                self.source_transport_descriptor_sha256,
                "source_transport_descriptor_sha256",
            ),
            (
                self.source_transport_binding_sha256,
                "source_transport_binding_sha256",
            ),
            (self.source_r22_admitted_plan_sha256, "source_r22_admitted_plan_sha256"),
            (self.execution_authority_sha256, "execution_authority_sha256"),
        ):
            _require_sha256(digest_value, name)
        operations = cast(
            tuple[RuntimeVerticalOperationV1, ...],
            _require_exact_tuple(self.operations, RuntimeVerticalOperationV1, "operations"),
        )
        for values, name in (
            ((item.operation_id for item in operations), "operation IDs"),
            ((item.decision_id for item in operations), "operation decision IDs"),
            ((item.target_id for item in operations), "operation target IDs"),
        ):
            materialized = tuple(values)
            if len(materialized) != len(set(materialized)):
                raise R24ContractError("DUPLICATE_RUNTIME_ID", f"{name} repeat")
        if type(self.execution_scope) is not RuntimeVerticalExecutionScope:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "execution scope is untrusted")
        if (
            self.execution_scope is RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE
            and self.execution_authority_sha256 != CPU_FAKE_ACTIVE_AUTHORITY_SHA256
        ):
            raise R24ContractError(
                "CPU_FAKE_AUTHORITY_REQUIRED", "CPU fake plan binds another authority"
            )


@dataclass(frozen=True, slots=True)
class RuntimeVerticalPolicyOutputV1:
    policy_id: str
    status: RuntimeVerticalStatus
    decisions: tuple[RuntimeVerticalDecisionV1, ...]
    admitted_plan: RuntimeVerticalAdmittedPlanV1
    source_policy_output_sha256: str
    source_policy_receipt_sha256: str
    source_transport_descriptor_sha256: str
    validation_checks: tuple[str, ...]
    source_transport_binding_sha256: str | None = None
    execution_authority_sha256: str = CPU_FAKE_ACTIVE_AUTHORITY_SHA256
    execution_scope: RuntimeVerticalExecutionScope = RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE
    schema_version: str = RUNTIME_VERTICAL_OUTPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != (
            RUNTIME_VERTICAL_OUTPUT_SCHEMA_VERSION
        ):
            raise R24ContractError("UNKNOWN_SCHEMA_VERSION", "unknown vertical output schema")
        _require_runtime_id(self.policy_id, "policy_id")
        if type(self.status) is not RuntimeVerticalStatus:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "vertical status is untrusted")
        decisions = cast(
            tuple[RuntimeVerticalDecisionV1, ...],
            _require_exact_tuple(self.decisions, RuntimeVerticalDecisionV1, "decisions"),
        )
        if len({item.decision_id for item in decisions}) != len(decisions):
            raise R24ContractError("DUPLICATE_RUNTIME_ID", "decision IDs repeat")
        if len({item.target_id for item in decisions}) != len(decisions):
            raise R24ContractError("DUPLICATE_RUNTIME_ID", "decision target IDs repeat")
        if type(self.admitted_plan) is not RuntimeVerticalAdmittedPlanV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "admitted plan is untrusted")
        _require_sha256(self.source_policy_output_sha256, "source_policy_output_sha256")
        _require_sha256(self.source_policy_receipt_sha256, "source_policy_receipt_sha256")
        if self.source_transport_binding_sha256 is None:
            object.__setattr__(
                self,
                "source_transport_binding_sha256",
                self.source_transport_descriptor_sha256,
            )
        _require_sha256(
            self.source_transport_descriptor_sha256,
            "source_transport_descriptor_sha256",
        )
        _require_sha256(
            self.source_transport_binding_sha256,
            "source_transport_binding_sha256",
        )
        _require_sha256(self.execution_authority_sha256, "execution_authority_sha256")
        _require_checks(self.validation_checks)
        if type(self.execution_scope) is not RuntimeVerticalExecutionScope:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "execution scope is untrusted")
        if (
            self.execution_scope is RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE
            and self.execution_authority_sha256 != CPU_FAKE_ACTIVE_AUTHORITY_SHA256
        ):
            raise R24ContractError(
                "CPU_FAKE_AUTHORITY_REQUIRED", "CPU fake output binds another authority"
            )
        plan = self.admitted_plan
        if plan.execution_scope is not self.execution_scope:
            raise R24ContractError("EXECUTION_SCOPE_MISMATCH", "plan and output scopes differ")
        if plan.execution_authority_sha256 != self.execution_authority_sha256:
            raise R24ContractError(
                "EXECUTION_AUTHORITY_MISMATCH", "plan and output authorities differ"
            )
        if (
            plan.source_policy_output_sha256 != self.source_policy_output_sha256
            or plan.source_policy_receipt_sha256 != self.source_policy_receipt_sha256
            or plan.source_transport_descriptor_sha256 != self.source_transport_descriptor_sha256
            or plan.source_transport_binding_sha256 != self.source_transport_binding_sha256
        ):
            raise R24ContractError(
                "SOURCE_POLICY_BINDING_MISMATCH", "plan and output bind different R2.2 values"
            )
        material = {
            item.decision_id: item
            for item in decisions
            if item.operation in {RuntimeOperationKind.DROP, RuntimeOperationKind.REPLACE}
        }
        if set(material) != {item.decision_id for item in plan.operations}:
            raise R24ContractError(
                "ADMITTED_OPERATION_CENSUS_MISMATCH",
                "plan must contain every and only material decision",
            )
        for operation in plan.operations:
            decision = material[operation.decision_id]
            if (
                operation.target_id != decision.target_id
                or operation.kind is not decision.operation
            ):
                raise R24ContractError(
                    "ADMITTED_OPERATION_BINDING_MISMATCH",
                    "plan operation differs from its decision",
                )
        if self.status is RuntimeVerticalStatus.NO_ELIGIBLE_HISTORY:
            if decisions or plan.operations:
                raise R24ContractError(
                    "ZERO_TARGET_STATUS_MISMATCH",
                    "NO_ELIGIBLE_HISTORY must not invent a decision or operation",
                )
        elif not decisions:
            raise R24ContractError(
                "ZERO_TARGET_STATUS_MISMATCH", "non-empty evaluation status needs decisions"
            )

    @property
    def receipt_decision_kinds(self) -> tuple[SentinelDecisionKind, ...]:
        """Return only real target decisions; zero-target remains an empty census."""

        if self.status is RuntimeVerticalStatus.NO_ELIGIBLE_HISTORY:
            return ()
        return tuple(SentinelDecisionKind(item.operation.value) for item in self.decisions)

    @property
    def receipt_bridge_status(self) -> RuntimeVerticalStatus:
        """Versioned no-op status for a future R2.4 outer-receipt bridge."""

        return self.status


@dataclass(frozen=True, slots=True)
class RuntimeVerticalReceiptBridgeV1:
    """Honest bridge for R2.1 receipt paths that require a non-empty census.

    This value is additive and never serialized as an R2.1 v1 receipt.  Both
    no-history and zero-target cases retain an empty decision census.
    """

    logical_call_id: str
    status: RuntimeVerticalBridgeStatus
    policy_evaluated: bool
    target_count: int
    decision_kinds: tuple[SentinelDecisionKind, ...]
    policy_output_sha256: str | None
    schema_version: str = RUNTIME_VERTICAL_RECEIPT_BRIDGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != (
            RUNTIME_VERTICAL_RECEIPT_BRIDGE_SCHEMA_VERSION
        ):
            raise R24ContractError("UNKNOWN_SCHEMA_VERSION", "unknown receipt bridge schema")
        _require_runtime_id(self.logical_call_id, "logical_call_id")
        if type(self.status) is not RuntimeVerticalBridgeStatus:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "bridge status is untrusted")
        if type(cast(object, self.policy_evaluated)) is not bool:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "policy_evaluated is not bool")
        if type(cast(object, self.target_count)) is not int or self.target_count < 0:
            raise R24ContractError("INVALID_TARGET_COUNT", "target_count is invalid")
        if type(self.decision_kinds) is not tuple or any(
            type(item) is not SentinelDecisionKind for item in self.decision_kinds
        ):
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "decision census is untrusted")
        if self.policy_output_sha256 is not None:
            _require_sha256(self.policy_output_sha256, "policy_output_sha256")
        if self.status is RuntimeVerticalBridgeStatus.NO_HISTORY_AVAILABLE:
            if (
                self.policy_evaluated
                or self.target_count != 0
                or self.decision_kinds
                or self.policy_output_sha256 is not None
            ):
                raise R24ContractError(
                    "NO_HISTORY_BRIDGE_MISMATCH",
                    "no-history must bypass policy with an empty census",
                )
        elif self.status is RuntimeVerticalBridgeStatus.NO_ELIGIBLE_HISTORY:
            if (
                not self.policy_evaluated
                or self.target_count != 0
                or self.decision_kinds
                or self.policy_output_sha256 is None
            ):
                raise R24ContractError(
                    "ZERO_TARGET_BRIDGE_MISMATCH",
                    "zero-target must retain evaluated output and an empty census",
                )
        elif (
            not self.policy_evaluated
            or self.target_count <= 0
            or len(self.decision_kinds) != self.target_count
            or self.policy_output_sha256 is None
        ):
            raise R24ContractError(
                "EVALUATED_BRIDGE_MISMATCH",
                "evaluated bridge must bind every real target decision",
            )

    @classmethod
    def no_history(cls, logical_call_id: str) -> RuntimeVerticalReceiptBridgeV1:
        return cls(
            logical_call_id=logical_call_id,
            status=RuntimeVerticalBridgeStatus.NO_HISTORY_AVAILABLE,
            policy_evaluated=False,
            target_count=0,
            decision_kinds=(),
            policy_output_sha256=None,
        )

    @classmethod
    def from_policy_output(
        cls,
        logical_call_id: str,
        output: RuntimeVerticalPolicyOutputV1,
    ) -> RuntimeVerticalReceiptBridgeV1:
        trusted = snapshot_vertical_output(output)
        return cls(
            logical_call_id=logical_call_id,
            status=(
                RuntimeVerticalBridgeStatus.NO_ELIGIBLE_HISTORY
                if trusted.status is RuntimeVerticalStatus.NO_ELIGIBLE_HISTORY
                else RuntimeVerticalBridgeStatus.EVALUATED
            ),
            policy_evaluated=True,
            target_count=len(trusted.decisions),
            decision_kinds=trusted.receipt_decision_kinds,
            policy_output_sha256=vertical_output_sha256(trusted),
        )


def _snapshot_sentinel_receipt(value: SentinelReceipt) -> SentinelReceipt:
    if type(value) is not SentinelReceipt:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "base receipt must use the exact type")
    return SentinelReceipt(
        **{field.name: getattr(value, field.name) for field in fields(SentinelReceipt)}
    )


def _snapshot_sentinel_result(value: SentinelResult) -> SentinelResult:
    if type(value) is not SentinelResult:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "base result must use the exact type")
    return SentinelResult(
        receipt=_snapshot_sentinel_receipt(value.receipt),
        _raw_request_json=canonical_json_bytes(value.raw_request),
        _candidate_request_json=canonical_json_bytes(value.candidate_request),
        _final_request_json=canonical_json_bytes(value.final_request),
    )


@dataclass(frozen=True, slots=True, init=False)
class RuntimeVerticalSentinelResultV1:
    """Detached additive R2.4 result that preserves the R2.1 transport API."""

    _base_result: SentinelResult
    _bridge: RuntimeVerticalReceiptBridgeV1
    overlay_declaration_sha256: str | None
    schema_version: str

    def __init__(
        self,
        *,
        base_result: SentinelResult,
        bridge: RuntimeVerticalReceiptBridgeV1,
        overlay_declaration_sha256: str | None,
        schema_version: str = RUNTIME_VERTICAL_SENTINEL_RESULT_SCHEMA_VERSION,
    ) -> None:
        if type(schema_version) is not str or schema_version != (
            RUNTIME_VERTICAL_SENTINEL_RESULT_SCHEMA_VERSION
        ):
            raise R24ContractError("UNKNOWN_SCHEMA_VERSION", "unknown R2.4 result schema")
        base = _snapshot_sentinel_result(base_result)
        bridge_snapshot = snapshot_vertical_receipt_bridge(bridge)
        if overlay_declaration_sha256 is not None:
            _require_sha256(overlay_declaration_sha256, "overlay_declaration_sha256")
        if base.receipt.logical_call_id != bridge_snapshot.logical_call_id:
            raise R24ContractError(
                "LOGICAL_CALL_BINDING_MISMATCH", "base result and R2.4 bridge calls differ"
            )
        if bridge_snapshot.policy_evaluated:
            if (
                not base.receipt.policy_evaluated
                or base.receipt.policy_output_sha256 != bridge_snapshot.policy_output_sha256
            ):
                raise R24ContractError(
                    "POLICY_OUTPUT_BINDING_MISMATCH",
                    "base receipt and R2.4 bridge bind different evaluated output",
                )
        elif base.receipt.policy_evaluated:
            raise R24ContractError(
                "POLICY_EVALUATION_BINDING_MISMATCH",
                "no-history bridge cannot wrap an evaluated base receipt",
            )
        object.__setattr__(self, "_base_result", base)
        object.__setattr__(self, "_bridge", bridge_snapshot)
        object.__setattr__(self, "overlay_declaration_sha256", overlay_declaration_sha256)
        object.__setattr__(self, "schema_version", schema_version)

    @property
    def base_result(self) -> SentinelResult:
        return _snapshot_sentinel_result(self._base_result)

    @property
    def bridge(self) -> RuntimeVerticalReceiptBridgeV1:
        return snapshot_vertical_receipt_bridge(self._bridge)

    @property
    def receipt(self) -> SentinelReceipt:
        return _snapshot_sentinel_receipt(self._base_result.receipt)

    @property
    def raw_request(self) -> JsonValue:
        return snapshot_json_value(self._base_result.raw_request)

    @property
    def candidate_request(self) -> JsonValue:
        return snapshot_json_value(self._base_result.candidate_request)

    @property
    def final_request(self) -> JsonValue:
        return snapshot_json_value(self._base_result.final_request)

    @property
    def use_transformed_request(self) -> bool:
        return self._base_result.use_transformed_request


@runtime_checkable
class RuntimeVerticalPolicy(Protocol):
    """Exact R2.4 policy boundary; implementations return the trusted output type."""

    @property
    def policy_id(self) -> str: ...

    @property
    def execution_scope(self) -> RuntimeVerticalExecutionScope: ...

    @property
    def execution_authority_sha256(self) -> str: ...

    @property
    def source_transport_descriptor_sha256(self) -> str: ...

    @property
    def source_transport_binding_sha256(self) -> str: ...

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> RuntimeVerticalPolicyOutputV1: ...

    def evaluate_with_control(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        execution_control: PolicyExecutionControlV1,
    ) -> RuntimeVerticalPolicyOutputV1: ...


def vertical_decision_projection(value: RuntimeVerticalDecisionV1) -> dict[str, JsonValue]:
    if type(value) is not RuntimeVerticalDecisionV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "decision must use the exact type")
    return {
        "decision_id": value.decision_id,
        "target_id": value.target_id,
        "operation": value.operation.value,
        "source_decision_sha256": value.source_decision_sha256,
    }


def vertical_operation_projection(value: RuntimeVerticalOperationV1) -> dict[str, JsonValue]:
    if type(value) is not RuntimeVerticalOperationV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "operation must use the exact type")
    return {
        "operation_id": value.operation_id,
        "decision_id": value.decision_id,
        "target_id": value.target_id,
        "target_record_id": value.target_record_id,
        "target_span_sha256": value.target_span_sha256,
        "kind": value.kind.value,
        "source_operation_sha256": value.source_operation_sha256,
        "replacement_template": (
            None if value.replacement_template is None else value.replacement_template.value
        ),
    }


def vertical_plan_projection(value: RuntimeVerticalAdmittedPlanV1) -> dict[str, JsonValue]:
    if type(value) is not RuntimeVerticalAdmittedPlanV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "plan must use the exact type")
    return {
        "schema_version": value.schema_version,
        "plan_id": value.plan_id,
        "logical_call_id": value.logical_call_id,
        "host_id": value.host_id,
        "history_family": value.history_family,
        "history_codec_id": value.history_codec_id,
        "history_codec_contract_version": value.history_codec_contract_version,
        "source_request_sha256": value.source_request_sha256,
        "source_policy_output_sha256": value.source_policy_output_sha256,
        "source_policy_receipt_sha256": value.source_policy_receipt_sha256,
        "source_transport_descriptor_sha256": value.source_transport_descriptor_sha256,
        "source_transport_binding_sha256": value.source_transport_binding_sha256,
        "source_r22_admitted_plan_sha256": value.source_r22_admitted_plan_sha256,
        "execution_authority_sha256": value.execution_authority_sha256,
        "execution_scope": value.execution_scope.value,
        "operations": [vertical_operation_projection(item) for item in value.operations],
    }


def vertical_output_projection(value: RuntimeVerticalPolicyOutputV1) -> dict[str, JsonValue]:
    if type(value) is not RuntimeVerticalPolicyOutputV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "output must use the exact type")
    return {
        "schema_version": value.schema_version,
        "policy_id": value.policy_id,
        "status": value.status.value,
        "execution_scope": value.execution_scope.value,
        "source_policy_output_sha256": value.source_policy_output_sha256,
        "source_policy_receipt_sha256": value.source_policy_receipt_sha256,
        "source_transport_descriptor_sha256": value.source_transport_descriptor_sha256,
        "source_transport_binding_sha256": value.source_transport_binding_sha256,
        "execution_authority_sha256": value.execution_authority_sha256,
        "decisions": [vertical_decision_projection(item) for item in value.decisions],
        "admitted_plan": vertical_plan_projection(value.admitted_plan),
        "validation_checks": list(value.validation_checks),
    }


def vertical_receipt_bridge_projection(
    value: RuntimeVerticalReceiptBridgeV1,
) -> dict[str, JsonValue]:
    if type(value) is not RuntimeVerticalReceiptBridgeV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "bridge must use the exact type")
    return {
        "schema_version": value.schema_version,
        "logical_call_id": value.logical_call_id,
        "status": value.status.value,
        "policy_evaluated": value.policy_evaluated,
        "target_count": value.target_count,
        "decision_kinds": [item.value for item in value.decision_kinds],
        "policy_output_sha256": value.policy_output_sha256,
    }


def snapshot_vertical_receipt_bridge(
    value: RuntimeVerticalReceiptBridgeV1,
) -> RuntimeVerticalReceiptBridgeV1:
    if type(value) is not RuntimeVerticalReceiptBridgeV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "bridge must use the exact type")
    return RuntimeVerticalReceiptBridgeV1(
        logical_call_id=value.logical_call_id,
        status=value.status,
        policy_evaluated=value.policy_evaluated,
        target_count=value.target_count,
        decision_kinds=tuple(value.decision_kinds),
        policy_output_sha256=value.policy_output_sha256,
        schema_version=value.schema_version,
    )


def vertical_receipt_bridge_sha256(value: RuntimeVerticalReceiptBridgeV1) -> str:
    trusted = snapshot_vertical_receipt_bridge(value)
    return canonical_sha256(cast(JsonValue, vertical_receipt_bridge_projection(trusted)))


def snapshot_vertical_sentinel_result(
    value: RuntimeVerticalSentinelResultV1,
) -> RuntimeVerticalSentinelResultV1:
    if type(value) is not RuntimeVerticalSentinelResultV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "result must use the exact type")
    return RuntimeVerticalSentinelResultV1(
        base_result=value.base_result,
        bridge=value.bridge,
        overlay_declaration_sha256=value.overlay_declaration_sha256,
        schema_version=value.schema_version,
    )


def vertical_sentinel_result_projection(
    value: RuntimeVerticalSentinelResultV1,
) -> dict[str, JsonValue]:
    trusted = snapshot_vertical_sentinel_result(value)
    receipt = trusted.receipt
    return {
        "schema_version": trusted.schema_version,
        "logical_call_id": receipt.logical_call_id,
        "base_receipt_binding": {
            "configured_mode": receipt.configured_mode.value,
            "effective_mode": receipt.effective_mode.value,
            "validation_status": receipt.validation_status.value,
            "policy_output_sha256": receipt.policy_output_sha256,
            "raw_request_sha256": receipt.raw_request_sha256,
            "candidate_request_sha256": receipt.candidate_request_sha256,
            "final_request_sha256": receipt.final_request_sha256,
            "exact_diff_sha256": receipt.exact_diff_sha256,
        },
        "bridge_sha256": vertical_receipt_bridge_sha256(trusted.bridge),
        "overlay_declaration_sha256": trusted.overlay_declaration_sha256,
    }


def vertical_sentinel_result_sha256(value: RuntimeVerticalSentinelResultV1) -> str:
    return canonical_sha256(cast(JsonValue, vertical_sentinel_result_projection(value)))


def vertical_plan_sha256(value: RuntimeVerticalAdmittedPlanV1) -> str:
    return canonical_sha256(cast(JsonValue, vertical_plan_projection(value)))


def vertical_output_sha256(value: RuntimeVerticalPolicyOutputV1) -> str:
    return canonical_sha256(cast(JsonValue, vertical_output_projection(value)))


def _trusted_children(value: object) -> tuple[object, ...]:
    if type(value) is RuntimeVerticalDecisionV1:
        decision = value
        return (
            decision.decision_id,
            decision.target_id,
            decision.operation,
            decision.source_decision_sha256,
        )
    if type(value) is RuntimeVerticalOperationV1:
        operation = value
        return (
            operation.operation_id,
            operation.decision_id,
            operation.target_id,
            operation.target_record_id,
            operation.target_span_sha256,
            operation.kind,
            operation.source_operation_sha256,
            operation.replacement_template,
        )
    if type(value) is RuntimeVerticalAdmittedPlanV1:
        plan = value
        return (
            plan.plan_id,
            plan.logical_call_id,
            plan.host_id,
            plan.history_family,
            plan.history_codec_id,
            plan.history_codec_contract_version,
            plan.source_request_sha256,
            plan.source_policy_output_sha256,
            plan.source_policy_receipt_sha256,
            plan.source_transport_descriptor_sha256,
            plan.source_transport_binding_sha256,
            plan.source_r22_admitted_plan_sha256,
            plan.operations,
            plan.execution_authority_sha256,
            plan.execution_scope,
            plan.schema_version,
        )
    if type(value) is RuntimeVerticalPolicyOutputV1:
        output = value
        return (
            output.policy_id,
            output.status,
            output.decisions,
            output.admitted_plan,
            output.source_policy_output_sha256,
            output.source_policy_receipt_sha256,
            output.source_transport_descriptor_sha256,
            output.source_transport_binding_sha256,
            output.validation_checks,
            output.execution_authority_sha256,
            output.execution_scope,
            output.schema_version,
        )
    raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "snapshot graph has an unknown node")


def _validate_trusted_graph(value: object) -> None:
    trusted_records = {
        RuntimeVerticalDecisionV1,
        RuntimeVerticalOperationV1,
        RuntimeVerticalAdmittedPlanV1,
        RuntimeVerticalPolicyOutputV1,
    }
    trusted_enums = {
        RuntimeOperationKind,
        RuntimeReplacementTemplate,
        RuntimeVerticalExecutionScope,
        RuntimeVerticalStatus,
    }
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    visits = 0
    while stack:
        item, depth, exiting = stack.pop()
        if exiting:
            active.remove(id(item))
            continue
        visits += 1
        if visits > _MAX_GRAPH_VISITS:
            raise R24ContractError("GRAPH_NODE_LIMIT", "trusted graph visit budget exceeded")
        if depth > _MAX_GRAPH_DEPTH:
            raise R24ContractError("GRAPH_DEPTH_LIMIT", "trusted graph depth exceeded")
        if item is None or type(item) in {bool, int, float, str} or type(item) in trusted_enums:
            continue
        if type(item) is tuple:
            children = cast(tuple[object, ...], item)
        elif type(item) in trusted_records:
            children = _trusted_children(item)
        else:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "trusted graph has a foreign node")
        identity = id(item)
        if identity in active:
            raise R24ContractError("GRAPH_CYCLE", "trusted graph contains a cycle")
        active.add(identity)
        stack.append((item, depth, True))
        for child in reversed(children):
            stack.append((child, depth + 1, False))


def snapshot_vertical_plan(
    value: RuntimeVerticalAdmittedPlanV1,
) -> RuntimeVerticalAdmittedPlanV1:
    _validate_trusted_graph(value)
    if type(value) is not RuntimeVerticalAdmittedPlanV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "plan must use the exact type")
    operations = tuple(
        RuntimeVerticalOperationV1(
            operation_id=item.operation_id,
            decision_id=item.decision_id,
            target_id=item.target_id,
            target_record_id=item.target_record_id,
            target_span_sha256=item.target_span_sha256,
            kind=item.kind,
            source_operation_sha256=item.source_operation_sha256,
            replacement_template=item.replacement_template,
        )
        for item in value.operations
    )
    return RuntimeVerticalAdmittedPlanV1(
        plan_id=value.plan_id,
        logical_call_id=value.logical_call_id,
        host_id=value.host_id,
        history_family=value.history_family,
        history_codec_id=value.history_codec_id,
        history_codec_contract_version=value.history_codec_contract_version,
        source_request_sha256=value.source_request_sha256,
        source_policy_output_sha256=value.source_policy_output_sha256,
        source_policy_receipt_sha256=value.source_policy_receipt_sha256,
        source_transport_descriptor_sha256=value.source_transport_descriptor_sha256,
        source_transport_binding_sha256=value.source_transport_binding_sha256,
        source_r22_admitted_plan_sha256=value.source_r22_admitted_plan_sha256,
        operations=operations,
        execution_authority_sha256=value.execution_authority_sha256,
        execution_scope=value.execution_scope,
        schema_version=value.schema_version,
    )


def snapshot_vertical_output(
    value: RuntimeVerticalPolicyOutputV1,
) -> RuntimeVerticalPolicyOutputV1:
    _validate_trusted_graph(value)
    if type(value) is not RuntimeVerticalPolicyOutputV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "output must use the exact type")
    decisions = tuple(
        RuntimeVerticalDecisionV1(
            decision_id=item.decision_id,
            target_id=item.target_id,
            operation=item.operation,
            source_decision_sha256=item.source_decision_sha256,
        )
        for item in value.decisions
    )
    return RuntimeVerticalPolicyOutputV1(
        policy_id=value.policy_id,
        status=value.status,
        decisions=decisions,
        admitted_plan=snapshot_vertical_plan(value.admitted_plan),
        source_policy_output_sha256=value.source_policy_output_sha256,
        source_policy_receipt_sha256=value.source_policy_receipt_sha256,
        source_transport_descriptor_sha256=value.source_transport_descriptor_sha256,
        source_transport_binding_sha256=value.source_transport_binding_sha256,
        validation_checks=tuple(value.validation_checks),
        execution_authority_sha256=value.execution_authority_sha256,
        execution_scope=value.execution_scope,
        schema_version=value.schema_version,
    )


# Versioned aliases keep call sites readable without weakening exact-type checks.
RuntimeVerticalExecutionScopeV1 = RuntimeVerticalExecutionScope
RuntimeReplacementTemplateV1 = RuntimeReplacementTemplate
RuntimeVerticalPolicyV1 = RuntimeVerticalPolicy
RuntimeVerticalOuterResultV1 = RuntimeVerticalSentinelResultV1


__all__ = [
    "RUNTIME_VERTICAL_OUTPUT_SCHEMA_VERSION",
    "RUNTIME_VERTICAL_PLAN_SCHEMA_VERSION",
    "RUNTIME_VERTICAL_RECEIPT_BRIDGE_SCHEMA_VERSION",
    "RUNTIME_VERTICAL_SENTINEL_RESULT_SCHEMA_VERSION",
    "CPU_FAKE_ACTIVE_AUTHORITY_SHA256",
    "CpuFakeActiveAuthorityV1",
    "R24ContractError",
    "RuntimeReplacementTemplate",
    "RuntimeReplacementTemplateV1",
    "RuntimeVerticalAdmittedPlanV1",
    "RuntimeVerticalBridgeStatus",
    "RuntimeVerticalDecisionV1",
    "RuntimeVerticalExecutionScope",
    "RuntimeVerticalExecutionScopeV1",
    "RuntimeVerticalOperationV1",
    "RuntimeVerticalOuterResultV1",
    "RuntimeVerticalPolicy",
    "RuntimeVerticalPolicyOutputV1",
    "RuntimeVerticalPolicyV1",
    "RuntimeVerticalReceiptBridgeV1",
    "RuntimeVerticalSentinelResultV1",
    "RuntimeVerticalStatus",
    "canonical_sha256",
    "canonical_json_bytes",
    "cpu_fake_active_authority_projection",
    "cpu_fake_active_authority_sha256",
    "issue_cpu_fake_active_authority",
    "replacement_text_for_template",
    "snapshot_vertical_output",
    "snapshot_vertical_plan",
    "snapshot_vertical_receipt_bridge",
    "snapshot_vertical_sentinel_result",
    "snapshot_json_value",
    "vertical_decision_projection",
    "vertical_operation_projection",
    "vertical_output_projection",
    "vertical_output_sha256",
    "vertical_plan_projection",
    "vertical_plan_sha256",
    "vertical_receipt_bridge_projection",
    "vertical_receipt_bridge_sha256",
    "vertical_sentinel_result_projection",
    "vertical_sentinel_result_sha256",
]
