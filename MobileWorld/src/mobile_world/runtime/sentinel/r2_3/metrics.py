"""Low-cardinality runtime and offline-calibration metrics for R2.3."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

_OPERATIONS = (
    "TASK_START_GENERATE",
    "EXPLICIT_REVISION",
    "TRACK",
    "LINK_RELEVANCE",
    "COMPARE_TOPOLOGY",
)
_STATUSES = (
    "ADMITTED",
    "INPUT_REJECTED",
    "BACKEND_ERROR",
    "INVALID_RESPONSE",
    "ADMISSION_REJECTED",
    "STATE_CONFLICT",
    "SIDECAR_FAILURE",
    "INTERNAL_ERROR",
)
_MILESTONE_STATES = ("pending", "in_progress", "satisfied", "violated", "unknown")
_PATH_STATES = ("viable", "inactive", "unknown")
_RELEVANCE = ("active_path", "inactive_branch", "path_independent", "unknown")
_LATENCY_BUCKET_UPPER_NS = (
    100_000_000,
    250_000_000,
    500_000_000,
    1_000_000_000,
    2_500_000_000,
    5_000_000_000,
    10_000_000_000,
)


def _closed_label(value: object, choices: tuple[str, ...], name: str) -> str:
    if type(value) is not str or value not in choices:
        raise ValueError(f"{name} is not a closed R2.3 metric label")
    return value


@dataclass(frozen=True, slots=True)
class RubricRuntimeMetricV1:
    """One admitted or failed runtime operation with no high-cardinality labels."""

    operation: str
    status: str
    latency_ns: int
    backend_calls: int
    duplicate_cache_reuse: bool = False
    milestone_states: tuple[str, ...] = ()
    path_states: tuple[str, ...] = ()
    relevance: tuple[str, ...] = ()
    archive_shadow_count: int = 0

    def __post_init__(self) -> None:
        _closed_label(self.operation, _OPERATIONS, "operation")
        _closed_label(self.status, _STATUSES, "status")
        if type(self.latency_ns) is not int or self.latency_ns < 0:
            raise ValueError("latency_ns must be a non-negative exact integer")
        if type(self.backend_calls) is not int or self.backend_calls not in {0, 1}:
            raise ValueError("backend_calls must be zero or one")
        if type(self.duplicate_cache_reuse) is not bool:
            raise TypeError("duplicate_cache_reuse must be an exact bool")
        for values, choices, name in (
            (self.milestone_states, _MILESTONE_STATES, "milestone state"),
            (self.path_states, _PATH_STATES, "path state"),
            (self.relevance, _RELEVANCE, "record relevance"),
        ):
            if type(values) is not tuple:
                raise TypeError(f"{name} values must use an exact tuple")
            for value in values:
                _closed_label(value, choices, name)
        if (
            type(self.archive_shadow_count) is not int
            or self.archive_shadow_count < 0
            or self.archive_shadow_count > len(self.relevance)
        ):
            raise ValueError("archive_shadow_count is outside the relevance census")


@dataclass(frozen=True, slots=True)
class RubricCalibrationLabelsV1:
    """Explicit offline labels; the runtime rubric is never its own safety oracle."""

    label_set_sha256: str
    invented_requirement: bool | None = None
    false_completion: bool | None = None
    legal_alternative_false_deviation: bool | None = None
    false_archive: bool | None = None

    def __post_init__(self) -> None:
        if (
            type(self.label_set_sha256) is not str
            or len(self.label_set_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.label_set_sha256)
        ):
            raise ValueError("label_set_sha256 must be lowercase SHA-256")
        for name in (
            "invented_requirement",
            "false_completion",
            "legal_alternative_false_deviation",
            "false_archive",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be an exact bool or null")


@dataclass(frozen=True, slots=True)
class RubricMetricsSnapshotV1:
    runtime_operation_count: int
    operation_counts: tuple[tuple[str, int], ...]
    status_counts: tuple[tuple[str, int], ...]
    backend_call_count: int
    duplicate_cache_reuse_count: int
    milestone_state_counts: tuple[tuple[str, int], ...]
    path_state_counts: tuple[tuple[str, int], ...]
    relevance_counts: tuple[tuple[str, int], ...]
    archive_shadow_count: int
    unknown_or_abstain_count: int
    latency_count: int
    latency_sum_ns: int
    latency_max_ns: int
    latency_bucket_counts: tuple[tuple[int | None, int], ...]
    calibration_sample_count: int
    invented_requirement_evaluated: int
    invented_requirement_count: int
    completion_evaluated: int
    false_completion_count: int
    legal_alternative_evaluated: int
    legal_alternative_false_deviation_count: int
    archive_evaluated: int
    false_archive_count: int


class RubricMetricsV1:
    """Thread-safe counters with a fixed label universe."""

    def __init__(self) -> None:
        self._operation_counts = dict.fromkeys(_OPERATIONS, 0)
        self._status_counts = dict.fromkeys(_STATUSES, 0)
        self._milestone_states = dict.fromkeys(_MILESTONE_STATES, 0)
        self._path_states = dict.fromkeys(_PATH_STATES, 0)
        self._relevance = dict.fromkeys(_RELEVANCE, 0)
        self._latency_buckets = dict.fromkeys((*_LATENCY_BUCKET_UPPER_NS, None), 0)
        self._runtime_operation_count = 0
        self._backend_call_count = 0
        self._duplicate_cache_reuse_count = 0
        self._archive_shadow_count = 0
        self._unknown_or_abstain_count = 0
        self._latency_count = 0
        self._latency_sum_ns = 0
        self._latency_max_ns = 0
        self._calibration_sample_count = 0
        self._calibration_evaluated = {
            "invented_requirement": 0,
            "false_completion": 0,
            "legal_alternative_false_deviation": 0,
            "false_archive": 0,
        }
        self._calibration_errors = dict.fromkeys(self._calibration_evaluated, 0)
        self._lock = Lock()

    def record_runtime(self, event: RubricRuntimeMetricV1) -> None:
        if type(event) is not RubricRuntimeMetricV1:
            raise TypeError("event must be an exact RubricRuntimeMetricV1")
        with self._lock:
            self._runtime_operation_count += 1
            self._operation_counts[event.operation] += 1
            self._status_counts[event.status] += 1
            self._backend_call_count += event.backend_calls
            self._duplicate_cache_reuse_count += int(event.duplicate_cache_reuse)
            self._latency_count += 1
            self._latency_sum_ns += event.latency_ns
            self._latency_max_ns = max(self._latency_max_ns, event.latency_ns)
            for upper_bound in _LATENCY_BUCKET_UPPER_NS:
                if event.latency_ns <= upper_bound:
                    self._latency_buckets[upper_bound] += 1
            self._latency_buckets[None] += 1
            for state in event.milestone_states:
                self._milestone_states[state] += 1
                if state == "unknown":
                    self._unknown_or_abstain_count += 1
            for state in event.path_states:
                self._path_states[state] += 1
                if state == "unknown":
                    self._unknown_or_abstain_count += 1
            for relevance in event.relevance:
                self._relevance[relevance] += 1
                if relevance == "unknown":
                    self._unknown_or_abstain_count += 1
            self._archive_shadow_count += event.archive_shadow_count

    def record_calibration(self, labels: RubricCalibrationLabelsV1) -> None:
        if type(labels) is not RubricCalibrationLabelsV1:
            raise TypeError("labels must be exact offline calibration labels")
        with self._lock:
            self._calibration_sample_count += 1
            for name in self._calibration_evaluated:
                value = getattr(labels, name)
                if value is None:
                    continue
                self._calibration_evaluated[name] += 1
                self._calibration_errors[name] += int(value)

    def snapshot(self) -> RubricMetricsSnapshotV1:
        with self._lock:
            return RubricMetricsSnapshotV1(
                runtime_operation_count=self._runtime_operation_count,
                operation_counts=tuple(self._operation_counts.items()),
                status_counts=tuple(self._status_counts.items()),
                backend_call_count=self._backend_call_count,
                duplicate_cache_reuse_count=self._duplicate_cache_reuse_count,
                milestone_state_counts=tuple(self._milestone_states.items()),
                path_state_counts=tuple(self._path_states.items()),
                relevance_counts=tuple(self._relevance.items()),
                archive_shadow_count=self._archive_shadow_count,
                unknown_or_abstain_count=self._unknown_or_abstain_count,
                latency_count=self._latency_count,
                latency_sum_ns=self._latency_sum_ns,
                latency_max_ns=self._latency_max_ns,
                latency_bucket_counts=tuple(self._latency_buckets.items()),
                calibration_sample_count=self._calibration_sample_count,
                invented_requirement_evaluated=self._calibration_evaluated["invented_requirement"],
                invented_requirement_count=self._calibration_errors["invented_requirement"],
                completion_evaluated=self._calibration_evaluated["false_completion"],
                false_completion_count=self._calibration_errors["false_completion"],
                legal_alternative_evaluated=self._calibration_evaluated[
                    "legal_alternative_false_deviation"
                ],
                legal_alternative_false_deviation_count=self._calibration_errors[
                    "legal_alternative_false_deviation"
                ],
                archive_evaluated=self._calibration_evaluated["false_archive"],
                false_archive_count=self._calibration_errors["false_archive"],
            )


__all__ = [
    "RubricCalibrationLabelsV1",
    "RubricMetricsSnapshotV1",
    "RubricMetricsV1",
    "RubricRuntimeMetricV1",
]
