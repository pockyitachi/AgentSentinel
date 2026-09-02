"""Thread-safe, low-cardinality metrics for the R2.2 policy boundary."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from mobile_world.runtime.sentinel.r2_2.sidecar import PolicyEvaluationStatus

_VERDICTS = ("SUPPORTED", "REFUTED", "UNVERIFIABLE")
_TEMPORAL_VALIDITIES = ("ACTIVE", "INVALIDATED", "UNKNOWN", "N_A")
_OPERATIONS = ("KEEP", "DROP", "REPLACE", "KEEP_UNCERTAIN")
_LATENCY_BUCKET_UPPER_NS = (
    100_000_000,
    250_000_000,
    500_000_000,
    1_000_000_000,
    2_500_000_000,
    5_000_000_000,
    10_000_000_000,
)


@dataclass(frozen=True)
class PolicyDecisionMetricV1:
    """Only closed labels may enter metrics; no IDs or free text are accepted."""

    verdict: str
    temporal_validity: str
    operation: str

    def __post_init__(self) -> None:
        for name, value in (
            ("verdict", self.verdict),
            ("temporal_validity", self.temporal_validity),
            ("operation", self.operation),
        ):
            if type(value) is not str:
                raise TypeError(f"metric {name} must use an exact string")
        if self.verdict not in _VERDICTS:
            raise ValueError("metric verdict is not a closed R2.2 label")
        if self.temporal_validity not in _TEMPORAL_VALIDITIES:
            raise ValueError("metric temporal validity is not a closed R2.2 label")
        if self.operation not in _OPERATIONS:
            raise ValueError("metric operation is not a closed R2.2 label")


@dataclass(frozen=True)
class R22PolicyMetricsSnapshot:
    evaluation_count: int
    evaluation_status_counts: tuple[tuple[str, int], ...]
    target_count: int
    admitted_decision_count: int
    factual_verdict_counts: tuple[tuple[str, int], ...]
    temporal_validity_counts: tuple[tuple[str, int], ...]
    operation_counts: tuple[tuple[str, int], ...]
    material_edit_count: int
    abstain_count: int
    error_count: int
    latency_count: int
    latency_sum_ns: int
    latency_max_ns: int
    latency_bucket_counts: tuple[tuple[int | None, int], ...]

    @property
    def claim_coverage(self) -> float:
        if self.target_count == 0:
            return 0.0
        return self.admitted_decision_count / self.target_count


class R22PolicyMetrics:
    """In-memory metric accumulator with a deliberately fixed label universe."""

    def __init__(self) -> None:
        self._status = {item.value: 0 for item in PolicyEvaluationStatus}
        self._verdicts = dict.fromkeys(_VERDICTS, 0)
        self._temporal = dict.fromkeys(_TEMPORAL_VALIDITIES, 0)
        self._operations = dict.fromkeys(_OPERATIONS, 0)
        self._latency_buckets = dict.fromkeys((*_LATENCY_BUCKET_UPPER_NS, None), 0)
        self._evaluation_count = 0
        self._target_count = 0
        self._admitted_decision_count = 0
        self._material_edit_count = 0
        self._abstain_count = 0
        self._error_count = 0
        self._latency_count = 0
        self._latency_sum_ns = 0
        self._latency_max_ns = 0
        self._lock = Lock()

    def record(
        self,
        *,
        status: PolicyEvaluationStatus,
        latency_ns: int,
        target_count: int,
        admitted_decisions: tuple[PolicyDecisionMetricV1, ...] = (),
    ) -> bool:
        if type(status) is not PolicyEvaluationStatus:
            raise TypeError("status must be PolicyEvaluationStatus")
        if type(latency_ns) is not int or latency_ns < 0:
            raise ValueError("latency_ns must be a non-negative integer")
        if type(target_count) is not int or target_count < 0 or target_count > 256:
            raise ValueError("target_count must be an integer in the R2.2 schema bound")
        if type(admitted_decisions) is not tuple or any(
            type(item) is not PolicyDecisionMetricV1 for item in admitted_decisions
        ):
            raise TypeError("admitted_decisions must contain exact metric decision values")
        if status is not PolicyEvaluationStatus.ADMITTED and admitted_decisions:
            raise ValueError("failed evaluations cannot contribute semantic decision metrics")
        if status is PolicyEvaluationStatus.ADMITTED and len(admitted_decisions) != target_count:
            raise ValueError("admitted decisions must equal the packet target count")
        detached_decisions = tuple(
            PolicyDecisionMetricV1(
                verdict=item.verdict,
                temporal_validity=item.temporal_validity,
                operation=item.operation,
            )
            for item in admitted_decisions
        )

        if not self._lock.acquire(blocking=False):
            return False
        try:
            self._evaluation_count += 1
            self._status[status.value] += 1
            self._target_count += target_count
            self._latency_count += 1
            self._latency_sum_ns += latency_ns
            self._latency_max_ns = max(self._latency_max_ns, latency_ns)
            for upper_bound in _LATENCY_BUCKET_UPPER_NS:
                if latency_ns <= upper_bound:
                    self._latency_buckets[upper_bound] += 1
            self._latency_buckets[None] += 1

            if status is not PolicyEvaluationStatus.ADMITTED:
                self._error_count += 1
                return True
            self._admitted_decision_count += len(detached_decisions)
            for decision in detached_decisions:
                self._verdicts[decision.verdict] += 1
                self._temporal[decision.temporal_validity] += 1
                self._operations[decision.operation] += 1
                if decision.operation in {"DROP", "REPLACE"}:
                    self._material_edit_count += 1
                if decision.operation == "KEEP_UNCERTAIN":
                    self._abstain_count += 1
            return True
        finally:
            self._lock.release()

    def snapshot(self) -> R22PolicyMetricsSnapshot:
        with self._lock:
            return R22PolicyMetricsSnapshot(
                evaluation_count=self._evaluation_count,
                evaluation_status_counts=tuple(self._status.items()),
                target_count=self._target_count,
                admitted_decision_count=self._admitted_decision_count,
                factual_verdict_counts=tuple(self._verdicts.items()),
                temporal_validity_counts=tuple(self._temporal.items()),
                operation_counts=tuple(self._operations.items()),
                material_edit_count=self._material_edit_count,
                abstain_count=self._abstain_count,
                error_count=self._error_count,
                latency_count=self._latency_count,
                latency_sum_ns=self._latency_sum_ns,
                latency_max_ns=self._latency_max_ns,
                latency_bucket_counts=tuple(self._latency_buckets.items()),
            )


__all__ = [
    "PolicyDecisionMetricV1",
    "R22PolicyMetrics",
    "R22PolicyMetricsSnapshot",
]
