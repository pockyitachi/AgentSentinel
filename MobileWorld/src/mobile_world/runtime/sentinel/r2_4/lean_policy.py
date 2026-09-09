"""Explicit R2.2/R2.3 promotion for ordinary audited ``mw eval`` runs."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from copy import deepcopy
from threading import Lock
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonValue
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.prompt_view import (
    ExecutionStateViewV1,
    execution_state_view_sha256,
)
from mobile_world.runtime.sentinel.r2_2.contracts import (
    PolicyExecutionControlV1,
    RuntimeExecutionScope,
    RuntimeSentinelPolicyOutputV1,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56SentinelPolicy,
    bind_policy_execution_control,
    openai_responses_transport_binding_sha256,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    RuntimeVerticalExecutionScope,
    RuntimeVerticalPolicyOutputV1,
    canonical_sha256,
)
from mobile_world.runtime.sentinel.r2_4.execution_state import (
    build_execution_state_view,
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


class LeanPolicyPoisonedError(TimeoutError):
    """A prior unconfirmed timeout permanently disabled task-local Sentinel work."""


class LeanActiveRuntimePolicyV1:
    """Apply admitted policy edits plus the existing safe duplicate bridge."""

    execution_scope = RuntimeVerticalExecutionScope.LEAN_EVAL_ACTIVE

    def __init__(
        self,
        source_policy: GPT56SentinelPolicy[Any, Any],
        *,
        coordinator: R24RuntimeCoordinatorV1,
        history_failure_cleanup: Callable[[str], None] | None = None,
        execution_state_enabled: bool = False,
    ) -> None:
        if type(source_policy) is not GPT56SentinelPolicy:
            raise TypeError("lean ACTIVE requires the exact admitted GPT56 policy")
        if source_policy.execution_scope is not RuntimeExecutionScope.SHADOW_ONLY:
            raise TypeError("lean ACTIVE source must retain the R2.2 SHADOW_ONLY scope")
        if type(coordinator) is not R24RuntimeCoordinatorV1:
            raise TypeError("coordinator must use the exact R2.4 type")
        if history_failure_cleanup is not None and not callable(history_failure_cleanup):
            raise TypeError("history_failure_cleanup must be callable when supplied")
        if type(execution_state_enabled) is not bool:
            raise TypeError("execution_state_enabled must be an exact bool")
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
        self._history_failure_cleanup = history_failure_cleanup
        self._execution_state_enabled = execution_state_enabled
        self._source_transport_descriptor_sha256 = descriptor_sha256
        self._source_transport_binding_sha256 = binding_sha256
        self._execution_authority_sha256 = authority
        # The coordinator, rubric session, provider context store, and admission
        # bridge are task-local state machines. Keep one logical call in that
        # stateful path at a time.
        self._evaluation_lock = Lock()
        # The seam holds this separate gate in its caller thread while its
        # policy worker runs. A timeout poisons this task-local policy before
        # the gate is released, so later calls fail open without starting work.
        self._logical_call_gate = Lock()
        self._poison_lock = Lock()
        self._poisoned = False
        self._execution_state_view_lock = Lock()
        self._execution_state_views: dict[str, tuple[ExecutionStateViewV1, str]] = {}

    def _require_healthy(self) -> None:
        with self._poison_lock:
            if self._poisoned:
                raise LeanPolicyPoisonedError(
                    "lean Sentinel was disabled after an unconfirmed timeout"
                )

    def poison(self) -> None:
        """Permanently disable this task-local policy after a stuck worker."""

        with self._poison_lock:
            self._poisoned = True

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

    def run_with_logical_call_gate[T](self, call: Callable[[], T]) -> T:
        if not callable(call):
            raise TypeError("logical-call gate requires a callable")
        with self._logical_call_gate:
            self._require_healthy()
            return call()

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
        promoted = (
            output if prune_plan is None else apply_r23_redundant_history_prunes(output, prune_plan)
        )
        if self._execution_state_enabled:
            evidence_input = self._coordinator.history_evidence_input_for(logical_call_id)
            if evidence_input is None or (
                evidence_input.packet_sha256 != source.admitted_plan.evidence_packet_sha256
            ):
                raise RuntimeError("history output differs from the prebuilt Collector packet")
            state_view = self.execution_state_view_for(logical_call_id)
            if state_view is not None and (
                state_view.source_request_sha256 != promoted.admitted_plan.source_request_sha256
            ):
                raise RuntimeError("history output differs from the frozen execution-state request")
        return promoted

    def _capture_execution_state_view(
        self,
        *,
        logical_call_id: str,
        source_request_sha256: str,
        expected_evidence_packet_sha256: str | None,
    ) -> None:
        """Store Collector-only state independently of history-policy admission."""

        ledger = self._coordinator.execution_ledger_for(logical_call_id)
        evidence_input = self._coordinator.history_evidence_input_for(logical_call_id)
        with self._execution_state_view_lock:
            self._execution_state_views.pop(logical_call_id, None)
        if ledger is not None:
            if evidence_input is None:
                raise RuntimeError("execution ledger has no same-call evidence packet")
            packet = evidence_input.packet
            if (
                (
                    expected_evidence_packet_sha256 is not None
                    and evidence_input.packet_sha256 != expected_evidence_packet_sha256
                )
                or packet.logical_call_id != logical_call_id
                or packet.raw_request_sha256 != source_request_sha256
                or ledger.run_id != packet.cutoff.run_id
                or ledger.task_run_id != packet.cutoff.task_run_id
                or ledger.cutoff_step_id != packet.cutoff.step_id
                or ledger.cutoff_event_id != packet.cutoff.current_observation_event_id
                or ledger.cutoff_event_seq != packet.cutoff.cutoff_event_seq
            ):
                raise RuntimeError(
                    "execution ledger differs from the admitted same-call evidence packet"
                )
        state_view = (
            None
            if ledger is None
            else build_execution_state_view(
                ledger=ledger,
                logical_call_id=logical_call_id,
                source_request_sha256=source_request_sha256,
                rubric_state_changed=self._coordinator.rubric_state_changed_for(logical_call_id),
            )
        )
        with self._execution_state_view_lock:
            if state_view is not None:
                snapshot = deepcopy(state_view)
                self._execution_state_views[logical_call_id] = (
                    snapshot,
                    execution_state_view_sha256(snapshot),
                )

    def _prepare_execution_state_before_policy(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> None:
        """Freeze local Collector facts before any history/rubric provider work."""

        if not self._execution_state_enabled:
            return
        try:
            self._coordinator.prepare_execution_state(request, context, history_ir)
        except Exception:
            # State is an independent best-effort channel.  History policy will
            # build its normal evidence (and surface its own typed failure) below.
            return
        try:
            self._capture_execution_state_view(
                logical_call_id=context.logical_call_id,
                source_request_sha256=canonical_sha256(request),
                expected_evidence_packet_sha256=None,
            )
        except Exception:
            # A projection failure must not prevent the history channel from
            # consuming the already validated same-cutoff Collector bundle.
            with self._execution_state_view_lock:
                self._execution_state_views.pop(context.logical_call_id, None)

    def _cleanup_failed_history_call(self, logical_call_id: str) -> None:
        try:
            self._coordinator.discard_prepared_call(logical_call_id)
        except Exception:
            # This is bounded task-local memory cleanup, not policy authority.
            pass
        cleanup = self._history_failure_cleanup
        if cleanup is None:
            return
        try:
            cleanup(logical_call_id)
        except Exception:
            # Cleanup is memory hygiene only and must not replace the typed
            # history-policy failure that the caller will persist.
            pass

    def discard_history_call(self, logical_call_id: str) -> None:
        """Non-blocking cleanup after the seam has timed out this call."""

        if type(logical_call_id) is not str or not logical_call_id:
            raise TypeError("logical_call_id must be non-empty exact text")
        try:
            self._coordinator.discard_prepared_call(logical_call_id)
        except Exception:
            # Never invoke an arbitrary embedding callback on the timeout
            # caller.  The worker performs full cleanup if/when it exits.
            pass

    def execution_state_view_for(self, logical_call_id: str) -> ExecutionStateViewV1 | None:
        """Return an integrity-checked detached actor-visible state projection."""

        if type(logical_call_id) is not str or not logical_call_id:
            raise TypeError("logical_call_id must be non-empty exact text")
        with self._execution_state_view_lock:
            stored = self._execution_state_views.get(logical_call_id)
            if stored is None:
                return None
            value, expected_sha256 = stored
            if execution_state_view_sha256(value) != expected_sha256:
                raise RuntimeError("stored execution-state view failed its integrity check")
            snapshot = deepcopy(value)
        if execution_state_view_sha256(snapshot) != expected_sha256:
            raise RuntimeError("detached execution-state view failed its integrity check")
        return snapshot

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> RuntimeVerticalPolicyOutputV1:
        request_copy, context_copy, history_copy = self._inputs(request, context, history_ir)
        with self._evaluation_lock:
            self._require_healthy()
            self._prepare_execution_state_before_policy(
                request=request_copy,
                context=context_copy,
                history_ir=history_copy,
            )
            try:
                source = self._source_policy.evaluate(
                    request=request_copy,
                    context=context_copy,
                    history_ir=history_copy,
                )
            except Exception:
                self._cleanup_failed_history_call(context.logical_call_id)
                raise
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
        with self._evaluation_lock:
            self._require_healthy()
            self._prepare_execution_state_before_policy(
                request=request_copy,
                context=context_copy,
                history_ir=history_copy,
            )
            try:
                with bind_policy_execution_control(execution_control):
                    source = self._source_policy.evaluate_with_control(
                        request=request_copy,
                        context=context_copy,
                        history_ir=history_copy,
                        execution_control=execution_control,
                    )
            except Exception:
                self._cleanup_failed_history_call(context.logical_call_id)
                raise
            self._require_healthy()
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
        with self._evaluation_lock:
            self._require_healthy()
            with bind_policy_execution_control(execution_control):
                result = self._coordinator.prepare_no_history(deepcopy(request), deepcopy(context))
            self._require_healthy()
            return result


__all__ = ["LeanActiveRuntimePolicyV1", "LeanPolicyPoisonedError"]
