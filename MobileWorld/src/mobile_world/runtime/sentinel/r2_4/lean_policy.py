"""Explicit R2.2/R2.3 promotion for ordinary audited ``mw eval`` runs."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonValue
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.r2_2.contracts import (
    PolicyExecutionControlV1,
    RuntimeExecutionScope,
    RuntimeSentinelPolicyOutputV1,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56SentinelPolicy,
    openai_responses_transport_binding_sha256,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    RuntimeVerticalExecutionScope,
    RuntimeVerticalPolicyOutputV1,
    canonical_sha256,
)
from mobile_world.runtime.sentinel.r2_4.orchestration import (
    R24CoordinatedCallRecordV1,
    R24RuntimeCoordinatorV1,
)
from mobile_world.runtime.sentinel.r2_4.policy import (
    apply_r23_redundant_history_prunes,
    promote_r22_policy_output,
)

_LEAN_PROMOTION_CHECKS = (
    "R24_LEAN_EVAL_CONFIGURATION_BOUND",
    "R24_LEAN_DIRECT_APPLICATION",
    "R24_SOURCE_TRANSPORT_DESCRIPTOR_BOUND",
    "R24_SOURCE_TRANSPORT_BINDING_BOUND",
    "R24_NO_ACTION_OR_TOOL_AUTHORITY",
)


class LeanActiveRuntimePolicyV1:
    """Apply admitted policy edits plus the existing safe duplicate bridge."""

    execution_scope = RuntimeVerticalExecutionScope.LEAN_EVAL_ACTIVE

    def __init__(
        self,
        source_policy: GPT56SentinelPolicy[Any, Any],
        *,
        coordinator: R24RuntimeCoordinatorV1,
    ) -> None:
        if type(source_policy) is not GPT56SentinelPolicy:
            raise TypeError("lean ACTIVE requires the exact admitted GPT56 policy")
        if source_policy.execution_scope is not RuntimeExecutionScope.SHADOW_ONLY:
            raise TypeError("lean ACTIVE source must retain the R2.2 SHADOW_ONLY scope")
        if type(coordinator) is not R24RuntimeCoordinatorV1:
            raise TypeError("coordinator must use the exact R2.4 type")
        descriptor = source_policy.transport_descriptor
        descriptor_sha256 = source_policy.transport_descriptor_sha256
        if descriptor.transport_kind == "OPENAI_RESPONSES":
            binding_sha256 = openai_responses_transport_binding_sha256(
                source_policy.assert_live_transport_binding()
            )
        else:
            binding_sha256 = descriptor_sha256
        authority = canonical_sha256(
            cast(
                JsonValue,
                {
                    "execution_scope": self.execution_scope.value,
                    "policy_id": source_policy.policy_id,
                    "source_transport_binding_sha256": binding_sha256,
                    "source_transport_descriptor_sha256": descriptor_sha256,
                },
            )
        )
        subject = f"{source_policy.policy_id}\0{authority}".encode()
        self._policy_id = f"r24-lean-{hashlib.sha256(subject).hexdigest()[:32]}"
        self._source_policy = source_policy
        self._coordinator = coordinator
        self._source_transport_descriptor_sha256 = descriptor_sha256
        self._source_transport_binding_sha256 = binding_sha256
        self._execution_authority_sha256 = authority

    @property
    def policy_id(self) -> str:
        return self._policy_id

    @property
    def source_transport_descriptor_sha256(self) -> str:
        return self._source_transport_descriptor_sha256

    @property
    def source_transport_binding_sha256(self) -> str:
        return self._source_transport_binding_sha256

    @property
    def execution_authority_sha256(self) -> str:
        return self._execution_authority_sha256

    @staticmethod
    def _inputs(
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> tuple[JsonValue, SentinelContext, HistoryIR]:
        if type(context) is not SentinelContext or type(history_ir) is not HistoryIR:
            raise TypeError("lean ACTIVE inputs must use exact runtime contracts")
        return deepcopy(request), deepcopy(context), deepcopy(history_ir)

    def _promote(
        self,
        source: RuntimeSentinelPolicyOutputV1,
        logical_call_id: str,
    ) -> RuntimeVerticalPolicyOutputV1:
        output = promote_r22_policy_output(
            source,
            policy_id=self._policy_id,
            source_transport_descriptor_sha256=self._source_transport_descriptor_sha256,
            source_transport_binding_sha256=self._source_transport_binding_sha256,
            execution_scope=self.execution_scope,
            execution_authority_sha256=self._execution_authority_sha256,
            validation_checks=_LEAN_PROMOTION_CHECKS,
        )
        prune_plan = self._coordinator.rubric_history_prune_plan_for(logical_call_id)
        return (
            output if prune_plan is None else apply_r23_redundant_history_prunes(output, prune_plan)
        )

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> RuntimeVerticalPolicyOutputV1:
        request_copy, context_copy, history_copy = self._inputs(request, context, history_ir)
        source = self._source_policy.evaluate(
            request=request_copy,
            context=context_copy,
            history_ir=history_copy,
        )
        return self._promote(source, context.logical_call_id)

    def evaluate_with_control(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        execution_control: PolicyExecutionControlV1,
    ) -> RuntimeVerticalPolicyOutputV1:
        request_copy, context_copy, history_copy = self._inputs(request, context, history_ir)
        source = self._source_policy.evaluate_with_control(
            request=request_copy,
            context=context_copy,
            history_ir=history_copy,
            execution_control=execution_control,
        )
        return self._promote(source, context.logical_call_id)

    def prepare_no_history_with_control(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        execution_control: PolicyExecutionControlV1,
    ) -> R24CoordinatedCallRecordV1:
        if not isinstance(execution_control, PolicyExecutionControlV1):
            raise TypeError("no-history rubric preparation needs the seam execution fence")
        return self._coordinator.prepare_no_history(deepcopy(request), deepcopy(context))


__all__ = ["LeanActiveRuntimePolicyV1"]
