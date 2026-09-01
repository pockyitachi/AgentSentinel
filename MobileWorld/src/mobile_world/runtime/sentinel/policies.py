"""Provider-free deterministic policy backends for the R2.1 CPU tranche."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonValue
from mobile_world.runtime.sentinel.contracts import (
    SentinelContext,
    SentinelDecision,
    SentinelDecisionKind,
    SentinelPolicyOutput,
)

PolicyFactory = Callable[[JsonValue, SentinelContext, HistoryIR], SentinelPolicyOutput]


class NoOpSentinelPolicy:
    """Deterministic abstaining backend; it never proposes a transformation."""

    policy_id = "mobileworld.runtime.sentinel-policy.noop/v1"

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> SentinelPolicyOutput:
        del request, history_ir
        return SentinelPolicyOutput(
            decisions=(
                SentinelDecision(
                    decision_id=f"{context.logical_call_id}:keep-uncertain",
                    kind=SentinelDecisionKind.KEEP_UNCERTAIN,
                    reason_code="R21_DETERMINISTIC_NOOP",
                ),
            ),
            transformation_plan=None,
        )


class DeterministicFakeSentinelPolicy:
    """Injectable CPU-only backend used to prove the seam and guards."""

    def __init__(
        self,
        factory: PolicyFactory,
        *,
        policy_id: str = "mobileworld.runtime.sentinel-policy.deterministic-fake/v1",
    ) -> None:
        if not callable(factory):
            raise TypeError("factory must be callable")
        if not policy_id:
            raise ValueError("policy_id is required")
        self._factory = factory
        self._policy_id = policy_id
        self._evaluate_count = 0
        self._lock = Lock()

    @property
    def policy_id(self) -> str:
        return self._policy_id

    @property
    def evaluate_count(self) -> int:
        with self._lock:
            return self._evaluate_count

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> SentinelPolicyOutput:
        with self._lock:
            self._evaluate_count += 1
        return self._factory(request, context, history_ir)


__all__ = ["DeterministicFakeSentinelPolicy", "NoOpSentinelPolicy", "PolicyFactory"]
