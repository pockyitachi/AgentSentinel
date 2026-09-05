"""CPU-fake R2.2-to-R2.4 policy promotion.

The adapter deliberately does not confer live authority.  It accepts the
R2.2 policy protocol, calls it with detached inputs, and promotes a fully
admitted R2.2 output only when an exact :class:`CpuFakeActiveAuthorityV1`
token is supplied.  Optional REPLACE coverage converts selected admitted DROP
operations to one closed Sentinel-authored template; backend text is never
accepted as a replacement payload.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonValue
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.r2_2.contracts import (
    EvidenceRelation,
    FactualVerdict,
    PolicyExecutionControlV1,
    RuntimeAdmittedOperationV1,
    RuntimeEvidencePolicyV1,
    RuntimeExecutionScope,
    RuntimeFallbackStatus,
    RuntimeOperationKind,
    RuntimeReasonCode,
    RuntimeSentinelPolicyOutputV1,
    TemporalValidity,
    runtime_admitted_operation_projection,
    runtime_admitted_plan_sha256,
    runtime_claim_proposal_projection,
    runtime_policy_output_sha256,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56SentinelPolicy,
    TransportDescriptorV1,
    transport_descriptor_sha256,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    CPU_FAKE_ACTIVE_AUTHORITY_SHA256,
    CpuFakeActiveAuthorityV1,
    R24ContractError,
    RuntimeReplacementTemplate,
    RuntimeVerticalAdmittedPlanV1,
    RuntimeVerticalDecisionV1,
    RuntimeVerticalExecutionScope,
    RuntimeVerticalOperationV1,
    RuntimeVerticalPolicyOutputV1,
    RuntimeVerticalStatus,
    canonical_sha256,
    cpu_fake_active_authority_sha256,
    snapshot_json_value,
    snapshot_vertical_output,
)

_PROMOTION_CHECKS = (
    "R24_R22_EXACT_OUTPUT_BOUND",
    "R24_R22_RECEIPT_HASH_RETAINED",
    "R24_CPU_FAKE_AUTHORITY_BOUND",
    "R24_TARGET_CENSUS_BOUND",
    "R24_FIXED_REPLACEMENT_TEMPLATE_ONLY",
    "R24_FIXED_REPLACEMENT_STRONG_EVIDENCE_BOUND",
    "R24_NO_ACTION_OR_TOOL_AUTHORITY",
    "R24_ZERO_TARGET_NOOP_VALID",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _projection_sha256(value: dict[str, JsonValue]) -> str:
    return canonical_sha256(cast(JsonValue, value))


def _stable_id(prefix: str, value: dict[str, JsonValue]) -> str:
    return f"{prefix}-{_projection_sha256(value)[:32]}"


def _validate_authority(value: object) -> CpuFakeActiveAuthorityV1:
    if type(value) is not CpuFakeActiveAuthorityV1:
        raise R24ContractError(
            "CPU_FAKE_AUTHORITY_REQUIRED", "promotion needs the exact offline authority token"
        )
    # Reconstructing re-runs the closed authority checks and detaches the value.
    return CpuFakeActiveAuthorityV1(
        offline=value.offline,
        fake_provider=value.fake_provider,
        network_allowed=value.network_allowed,
        gpu_allowed=value.gpu_allowed,
        actor_actions_allowed=value.actor_actions_allowed,
        scope=value.scope,
    )


def _validate_replace_targets(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value):
        raise R24ContractError(
            "UNTRUSTED_RUNTIME_TYPE", "replace target IDs must be an exact tuple of strings"
        )
    targets = cast(tuple[str, ...], value)
    if len(targets) > 256:
        raise R24ContractError("RUNTIME_COLLECTION_TOO_LARGE", "replace targets exceed 256")
    if len(targets) != len(set(targets)):
        raise R24ContractError("DUPLICATE_RUNTIME_ID", "replace target IDs repeat")
    return tuple(targets)


def _require_cpu_fake_source_policy(
    source_policy: RuntimeEvidencePolicyV1,
) -> tuple[TransportDescriptorV1, str]:
    """Read the construction-bound descriptor from the exact R2.2 GPT policy.

    This check intentionally rejects arbitrary protocol implementations: a
    structural policy can claim SHADOW while doing untracked live I/O.  The
    exact GPT56 policy snapshots its descriptor before any evaluation, so the
    fake/no-network/no-model declaration is a usable local authority boundary.
    """

    source_object: object = source_policy
    if type(source_object) is not GPT56SentinelPolicy:
        raise R24ContractError(
            "CPU_FAKE_SOURCE_ATTESTATION_REQUIRED",
            "only the exact descriptor-bound R2.2 GPT policy may be promoted",
        )
    trusted_policy = cast(Any, source_object)
    descriptor = trusted_policy.transport_descriptor
    reported_sha256 = trusted_policy.transport_descriptor_sha256
    if type(descriptor) is not TransportDescriptorV1:
        raise R24ContractError(
            "CPU_FAKE_SOURCE_ATTESTATION_REQUIRED", "source descriptor is missing"
        )
    projected = TransportDescriptorV1(
        transport_kind=descriptor.transport_kind,
        transport_authority=descriptor.transport_authority,
        openai_sdk_version=descriptor.openai_sdk_version,
        sdk_max_retries=descriptor.sdk_max_retries,
        external_network_on_call=descriptor.external_network_on_call,
        model_on_call=descriptor.model_on_call,
    )
    if (
        projected.transport_kind != "FAKE"
        or projected.transport_authority != "CPU_OFFLINE_FAKE"
        or projected.external_network_on_call
        or projected.model_on_call
        or projected.sdk_max_retries != 0
    ):
        raise R24ContractError(
            "CPU_FAKE_SOURCE_ATTESTATION_REQUIRED",
            "source policy transport is not CPU_OFFLINE_FAKE",
        )
    digest = transport_descriptor_sha256(projected)
    if type(reported_sha256) is not str or reported_sha256 != digest:
        raise R24ContractError(
            "SOURCE_AUTHORITY_DRIFT", "source descriptor property and hash property differ"
        )
    return projected, digest


def _validate_fixed_replacement_basis(decision: object) -> RuntimeReplacementTemplate:
    from mobile_world.runtime.sentinel.r2_2.contracts import RuntimeClaimProposalV1

    if type(decision) is not RuntimeClaimProposalV1:
        raise R24ContractError("UNTRUSTED_R22_OUTPUT", "replacement decision is untrusted")
    if decision.proposed_operation is not RuntimeOperationKind.DROP:
        raise R24ContractError(
            "REPLACEMENT_REQUIRES_ADMITTED_DROP",
            "the fixed template may replace only an R2.2-admitted DROP",
        )
    if decision.uncertainty_codes or decision.fallback_status is not RuntimeFallbackStatus.NONE:
        raise R24ContractError(
            "REPLACEMENT_EVIDENCE_INSUFFICIENT", "replacement cannot carry uncertainty/fallback"
        )
    relations = {item.relation for item in decision.evidence_refs}
    refuted = (
        decision.factual_verdict is FactualVerdict.REFUTED
        and decision.temporal_validity in {TemporalValidity.ACTIVE, TemporalValidity.NOT_APPLICABLE}
        and decision.reason_code is RuntimeReasonCode.DIRECT_EVIDENCE_REFUTATION
        and EvidenceRelation.REFUTES in relations
    )
    invalidated = (
        decision.factual_verdict is FactualVerdict.SUPPORTED
        and decision.temporal_validity is TemporalValidity.INVALIDATED
        and decision.reason_code is RuntimeReasonCode.LATER_EVIDENCE_INVALIDATES
        and {EvidenceRelation.SUPPORTS, EvidenceRelation.INVALIDATES} <= relations
    )
    if not (refuted or invalidated):
        raise R24ContractError(
            "REPLACEMENT_EVIDENCE_INSUFFICIENT",
            "fixed replacement needs refutation or later invalidation evidence",
        )
    if refuted:
        return RuntimeReplacementTemplate.REFUTED_HISTORY_FACT_V1
    return RuntimeReplacementTemplate.STALE_AFTER_INVALIDATION_V1


def promote_r22_policy_output(
    source: RuntimeSentinelPolicyOutputV1,
    *,
    policy_id: str,
    source_transport_descriptor_sha256: str,
    source_transport_binding_sha256: str | None = None,
    replace_drop_target_ids: tuple[str, ...] = (),
    authority: CpuFakeActiveAuthorityV1 | None = None,
    execution_scope: RuntimeVerticalExecutionScope = (
        RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE
    ),
    execution_authority_sha256: str | None = None,
    validation_checks: tuple[str, ...] | None = None,
) -> RuntimeVerticalPolicyOutputV1:
    """Project an exact admitted R2.2 output into an authority-bound overlay.

    This function constructs data; it never grants execution authority.  The
    common seam admits only an exact adapter that independently validates the
    corresponding CPU token or owner-authored live manifest.
    """

    if type(execution_scope) is not RuntimeVerticalExecutionScope:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "execution scope is untrusted")
    if execution_scope is RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE:
        trusted_authority = _validate_authority(authority)
        expected_authority_sha256 = cpu_fake_active_authority_sha256(trusted_authority)
        if execution_authority_sha256 is None:
            execution_authority_sha256 = expected_authority_sha256
        if execution_authority_sha256 != expected_authority_sha256 or (
            execution_authority_sha256 != CPU_FAKE_ACTIVE_AUTHORITY_SHA256
        ):
            raise R24ContractError(
                "CPU_FAKE_AUTHORITY_REQUIRED", "promotion binds another CPU fake authority"
            )
        resolved_checks = _PROMOTION_CHECKS if validation_checks is None else validation_checks
    else:
        if authority is not None:
            raise R24ContractError(
                "LIVE_AUTHORITY_MISMATCH", "CPU fake authority cannot authorize a live scope"
            )
        if (
            type(execution_authority_sha256) is not str
            or _SHA256.fullmatch(execution_authority_sha256) is None
        ):
            raise R24ContractError(
                "LIVE_AUTHORITY_REQUIRED", "live promotion requires the owner manifest hash"
            )
        if type(validation_checks) is not tuple or not {
            "R24_OWNER_AUTHORITY_MANIFEST_BOUND",
            "R24_LIVE_TRANSPORT_DESCRIPTOR_BOUND",
            "R24_LIVE_TRANSPORT_BINDING_BOUND",
        }.issubset(validation_checks):
            raise R24ContractError(
                "LIVE_AUTHORITY_REQUIRED", "live promotion checks do not bind its authorities"
            )
        resolved_checks = validation_checks
    if (
        type(source_transport_descriptor_sha256) is not str
        or _SHA256.fullmatch(source_transport_descriptor_sha256) is None
    ):
        raise R24ContractError(
            "CPU_FAKE_SOURCE_ATTESTATION_REQUIRED", "source descriptor hash is invalid"
        )
    if source_transport_binding_sha256 is None:
        source_transport_binding_sha256 = source_transport_descriptor_sha256
    if (
        type(source_transport_binding_sha256) is not str
        or _SHA256.fullmatch(source_transport_binding_sha256) is None
    ):
        raise R24ContractError(
            "SOURCE_TRANSPORT_BINDING_REQUIRED", "source transport binding hash is invalid"
        )
    replace_targets = frozenset(_validate_replace_targets(replace_drop_target_ids))
    if type(source) is not RuntimeSentinelPolicyOutputV1:
        raise R24ContractError(
            "UNTRUSTED_R22_OUTPUT", "source policy output must use the exact R2.2 type"
        )
    # Every nested R2.2 projector also requires exact trusted types.  Compute
    # these bindings before reading admission fields for promotion.
    try:
        source_output_sha256 = runtime_policy_output_sha256(source)
        source_plan_sha256 = runtime_admitted_plan_sha256(source.admitted_plan)
    except (TypeError, ValueError, RecursionError) as exc:
        raise R24ContractError(
            "UNTRUSTED_R22_OUTPUT", "source policy output failed trusted projection"
        ) from exc

    source_operations: dict[str, RuntimeAdmittedOperationV1] = {}
    for operation in source.admitted_plan.operations:
        if type(operation) is not RuntimeAdmittedOperationV1:
            raise R24ContractError("UNTRUSTED_R22_OUTPUT", "source admitted operation is untrusted")
        if operation.decision_id in source_operations:
            raise R24ContractError("DUPLICATE_RUNTIME_ID", "source operation decision IDs repeat")
        source_operations[operation.decision_id] = operation

    decisions: list[RuntimeVerticalDecisionV1] = []
    operations: list[RuntimeVerticalOperationV1] = []
    material_decisions: set[str] = set()
    source_target_ids = {item.target_id for item in source.decisions}
    unknown_replace_targets = replace_targets - source_target_ids
    if unknown_replace_targets:
        raise R24ContractError(
            "UNKNOWN_REPLACEMENT_TARGET", "fixed replacement target is absent from R2.2 output"
        )

    for decision in source.decisions:
        decision_hash = _projection_sha256(runtime_claim_proposal_projection(decision))
        kind = decision.proposed_operation
        if kind not in {
            RuntimeOperationKind.KEEP,
            RuntimeOperationKind.DROP,
            RuntimeOperationKind.KEEP_UNCERTAIN,
        }:
            raise R24ContractError(
                "UNTRUSTED_R22_OUTPUT", "R2.2 source operation is outside its admitted surface"
            )
        replacement_template: RuntimeReplacementTemplate | None = None
        if decision.target_id in replace_targets:
            replacement_template = _validate_fixed_replacement_basis(decision)
            kind = RuntimeOperationKind.REPLACE
        decisions.append(
            RuntimeVerticalDecisionV1(
                decision_id=decision.decision_id,
                target_id=decision.target_id,
                operation=kind,
                source_decision_sha256=decision_hash,
            )
        )
        if kind not in {RuntimeOperationKind.DROP, RuntimeOperationKind.REPLACE}:
            continue
        material_decisions.add(decision.decision_id)
        source_operation = source_operations.get(decision.decision_id)
        if source_operation is None or source_operation.kind is not RuntimeOperationKind.DROP:
            raise R24ContractError(
                "R22_ADMISSION_BINDING_MISMATCH",
                "material decision lacks its exact R2.2 admitted DROP",
            )
        operation_hash = _projection_sha256(runtime_admitted_operation_projection(source_operation))
        operation_subject: dict[str, JsonValue] = {
            "source_operation_sha256": operation_hash,
            "kind": kind.value,
            "replacement_template": (
                replacement_template.value if replacement_template is not None else None
            ),
        }
        operations.append(
            RuntimeVerticalOperationV1(
                operation_id=_stable_id("r24-op", operation_subject),
                decision_id=decision.decision_id,
                target_id=decision.target_id,
                target_record_id=source_operation.target_record_id,
                target_span_sha256=source_operation.target_span_sha256,
                kind=kind,
                source_operation_sha256=operation_hash,
                replacement_template=replacement_template,
            )
        )

    if set(source_operations) != material_decisions:
        raise R24ContractError(
            "R22_ADMISSION_BINDING_MISMATCH",
            "R2.2 admitted operation census differs from promoted decisions",
        )
    plan_subject: dict[str, JsonValue] = {
        "logical_call_id": source.admitted_plan.logical_call_id,
        "source_policy_output_sha256": source_output_sha256,
        "execution_scope": execution_scope.value,
        "execution_authority_sha256": execution_authority_sha256,
        "source_transport_binding_sha256": source_transport_binding_sha256,
        "operations": [item.operation_id for item in operations],
    }
    plan = RuntimeVerticalAdmittedPlanV1(
        plan_id=_stable_id("r24-plan", plan_subject),
        logical_call_id=source.admitted_plan.logical_call_id,
        host_id=source.admitted_plan.host_id,
        history_family=source.admitted_plan.history_family,
        history_codec_id=source.admitted_plan.history_codec_id,
        history_codec_contract_version=source.admitted_plan.history_codec_contract_version,
        source_request_sha256=source.admitted_plan.source_request_sha256,
        source_policy_output_sha256=source_output_sha256,
        source_policy_receipt_sha256=source.policy_receipt_sha256,
        source_transport_descriptor_sha256=source_transport_descriptor_sha256,
        source_transport_binding_sha256=source_transport_binding_sha256,
        source_r22_admitted_plan_sha256=source_plan_sha256,
        operations=tuple(operations),
        execution_authority_sha256=execution_authority_sha256,
        execution_scope=execution_scope,
    )
    output = RuntimeVerticalPolicyOutputV1(
        policy_id=policy_id,
        status=(
            RuntimeVerticalStatus.EVALUATED
            if decisions
            else RuntimeVerticalStatus.NO_ELIGIBLE_HISTORY
        ),
        decisions=tuple(decisions),
        admitted_plan=plan,
        source_policy_output_sha256=source_output_sha256,
        source_policy_receipt_sha256=source.policy_receipt_sha256,
        source_transport_descriptor_sha256=source_transport_descriptor_sha256,
        source_transport_binding_sha256=source_transport_binding_sha256,
        validation_checks=resolved_checks,
        execution_authority_sha256=execution_authority_sha256,
        execution_scope=execution_scope,
    )
    return snapshot_vertical_output(output)


class R22CpuFakeActivePolicyAdapter:
    """Adapt an injected R2.2 policy for offline ACTIVE request construction."""

    def __init__(
        self,
        source_policy: RuntimeEvidencePolicyV1,
        *,
        authority: CpuFakeActiveAuthorityV1,
        replace_drop_target_ids: tuple[str, ...] = (),
    ) -> None:
        self._authority = _validate_authority(authority)
        self._replace_drop_target_ids = _validate_replace_targets(replace_drop_target_ids)
        _descriptor, descriptor_sha256 = _require_cpu_fake_source_policy(source_policy)
        try:
            source_scope = source_policy.execution_scope
            source_policy_id = source_policy.policy_id
        except Exception as exc:
            raise R24ContractError(
                "UNTRUSTED_R22_POLICY", "source policy metadata could not be read"
            ) from exc
        if type(source_scope) is not RuntimeExecutionScope or (
            source_scope is not RuntimeExecutionScope.SHADOW_ONLY
        ):
            raise R24ContractError(
                "R22_SHADOW_SOURCE_REQUIRED", "adapter source must retain R2.2 SHADOW scope"
            )
        if type(source_policy_id) is not str or not source_policy_id:
            raise R24ContractError("UNTRUSTED_R22_POLICY", "source policy ID is invalid")
        self._source_policy = source_policy
        self._source_policy_id = source_policy_id
        self._source_descriptor_sha256 = descriptor_sha256
        self._execution_authority_sha256 = cpu_fake_active_authority_sha256(self._authority)
        self._policy_id = (
            f"r24-cpu-fake-{hashlib.sha256(source_policy_id.encode()).hexdigest()[:32]}"
        )

    @property
    def policy_id(self) -> str:
        return self._policy_id

    @property
    def source_policy_id(self) -> str:
        return self._source_policy_id

    @property
    def source_transport_descriptor_sha256(self) -> str:
        """Return the immutable construction-bound fake transport authority hash."""

        return self._source_descriptor_sha256

    @property
    def source_transport_binding_sha256(self) -> str:
        """The fake descriptor is the complete CPU transport binding."""

        return self._source_descriptor_sha256

    @property
    def execution_scope(self) -> RuntimeVerticalExecutionScope:
        return RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE

    @property
    def execution_authority_sha256(self) -> str:
        return self._execution_authority_sha256

    def _promote(self, value: RuntimeSentinelPolicyOutputV1) -> RuntimeVerticalPolicyOutputV1:
        return promote_r22_policy_output(
            value,
            policy_id=self._policy_id,
            authority=self._authority,
            source_transport_descriptor_sha256=self._source_descriptor_sha256,
            replace_drop_target_ids=self._replace_drop_target_ids,
        )

    def _require_source_authority_unchanged(self) -> None:
        _descriptor, digest = _require_cpu_fake_source_policy(self._source_policy)
        if digest != self._source_descriptor_sha256:
            raise R24ContractError(
                "SOURCE_AUTHORITY_DRIFT", "source transport descriptor changed after binding"
            )

    @staticmethod
    def _detached_inputs(
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> tuple[JsonValue, SentinelContext, HistoryIR]:
        if type(context) is not SentinelContext or type(history_ir) is not HistoryIR:
            raise R24ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "adapter inputs require exact trusted contracts"
            )
        request_copy = snapshot_json_value(request)
        # The common seam already builds an exact trusted HistoryIR snapshot.
        # A second deepcopy ensures the wrapped backend never receives the
        # authority graph later used by the caller for rendering/admission.
        try:
            context_copy = deepcopy(context)
            history_copy = deepcopy(history_ir)
        except (TypeError, ValueError, RecursionError) as exc:
            raise R24ContractError("GRAPH_SNAPSHOT_FAILED", "adapter input detach failed") from exc
        if type(context_copy) is not SentinelContext or type(history_copy) is not HistoryIR:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "adapter detach changed input types")
        return request_copy, context_copy, history_copy

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> RuntimeVerticalPolicyOutputV1:
        self._require_source_authority_unchanged()
        request_copy, context_copy, history_copy = self._detached_inputs(
            request, context, history_ir
        )
        source = self._source_policy.evaluate(
            request=request_copy,
            context=context_copy,
            history_ir=history_copy,
        )
        return self._promote(source)

    def evaluate_with_control(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        execution_control: PolicyExecutionControlV1,
    ) -> RuntimeVerticalPolicyOutputV1:
        self._require_source_authority_unchanged()
        request_copy, context_copy, history_copy = self._detached_inputs(
            request, context, history_ir
        )
        source = self._source_policy.evaluate_with_control(
            request=request_copy,
            context=context_copy,
            history_ir=history_copy,
            execution_control=execution_control,
        )
        return self._promote(source)


CpuFakeActiveRuntimePolicyAdapter = R22CpuFakeActivePolicyAdapter


__all__ = [
    "CpuFakeActiveRuntimePolicyAdapter",
    "R22CpuFakeActivePolicyAdapter",
    "promote_r22_policy_output",
]
