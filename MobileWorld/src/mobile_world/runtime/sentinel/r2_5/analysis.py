"""Fail-closed, CPU-only analysis contract for the R2.5 pilot.

The production driver deliberately records facts, not post-hoc semantic
labels.  This module turns the exact driver evidence and its hash-bound
per-decision audit details into denominator-explicit summaries.  It does not
read files, call a provider, inspect an environment, or infer that a failed
task makes a particular action or history edit wrong.

The following distinctions are intentional:

* request-hash changes, fallback, unsupported paths, exact duplicate executed
  actions, and an unsuccessful ``finished`` action are mechanically derived;
* unnecessary/wrong actions and wrong edits remain ``NOT_MEASURABLE`` without
  an independently admitted annotation;
* clean-history false-edit/false-archive rates have a zero measured
  denominator because R2.5 production evidence contains no independent
  clean-history label; and
* absent audit detail is counted as missing, never treated as a negative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import (
    canonical_sha256,
    snapshot_json_value,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    OFFICIAL_RESULT_EVALUATOR_ID_V1,
    PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
    PilotStageEvidenceV1,
    pilot_stage_evidence_projection,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FrozenPilotManifestV1,
    PilotArmV1,
    PilotHostV1,
    frozen_pilot_manifest_sha256,
)

PILOT_ANALYSIS_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.5-pilot-analysis/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")

_CENSUS_FIELDS = (
    "actor_actions",
    "actor_calls",
    "cost_usd_micros",
    "history_policy_openai_calls",
    "offline_rubric_evaluations",
    "openai_calls",
    "rubric_openai_calls",
    "wall_time_ms",
)
_STAGE_FIELDS = frozenset(
    {
        "actor_resources_sha256",
        "cells",
        "census",
        "history_policy_stage_sha256",
        "manifest_sha256",
        "pilot_manifest_sha256",
        "run_id",
        "schema_version",
    }
)
_CELL_FIELDS = frozenset(
    {
        "actor_resource_sha256",
        "arm",
        "census",
        "cleanup_receipt_sha256",
        "decisions",
        "effective_reset_state_sha256",
        "history_policy_stage_sha256",
        "host",
        "manifest_sha256",
        "official_result",
        "reset_receipt_sha256",
        "reset_seed",
        "run_id",
        "sentinel_mode",
        "sequence_index",
        "task_id",
        "task_parameters_sha256",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "actor_attempt_receipt_sha256",
        "actor_call_index",
        "case_execution_lease_sha256",
        "census",
        "exact_diff_sha256",
        "executed_action_sha256",
        "fallback_check",
        "fallback_reason",
        "final_request_sha256",
        "history_policy_attempt_receipt_sha256",
        "live_policy_authority_sha256",
        "live_policy_factory_binding_sha256",
        "logical_call_id",
        "parsed_action_sha256",
        "parser_result_sha256",
        "pre_provider_outcome",
        "pre_provider_status",
        "preflight_report_sha256",
        "provider_attempt_receipt_sha256",
        "provider_request_sha256",
        "provider_response_sha256",
        "raw_request_sha256",
        "rubric_attempt_receipt_sha256s",
        "runtime_audit_detail_sha256",
        "sentinel_receipt_sha256",
    }
)
_OFFICIAL_FIELDS = frozenset(
    {
        "evaluator_id",
        "reason_sha256",
        "result_payload_sha256",
        "score_ppm",
        "successful",
        "task_id",
    }
)
_CALL_RATE_METRICS: tuple[PilotRateMetricV1, ...]


class R25AnalysisContractError(ValueError):
    """Stable failure raised before publishing an invalid analysis."""

    def __init__(self, code: str, message: str) -> None:
        if type(code) is not str or _REASON.fullmatch(code) is None:
            raise ValueError("analysis errors require a closed reason code")
        self.code = code
        super().__init__(f"{code}: {message}")


class PilotMeasurementStatusV1(StrEnum):
    EXACT = "EXACT"
    PARTIAL = "PARTIAL"
    NOT_MEASURABLE = "NOT_MEASURABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PilotClassificationV1(StrEnum):
    OBSERVED = "OBSERVED"
    NOT_OBSERVED = "NOT_OBSERVED"
    UNKNOWN = "UNKNOWN"
    NOT_MEASURABLE = "NOT_MEASURABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PilotRateUnitV1(StrEnum):
    CELL = "CELL"
    ACTOR_CALL = "ACTOR_CALL"


class PilotRateMetricV1(StrEnum):
    OFFICIAL_SUCCESS = "OFFICIAL_SUCCESS"
    EDIT = "EDIT"
    ABSTAIN = "ABSTAIN"
    FALLBACK = "FALLBACK"
    ERROR = "ERROR"
    UNSUPPORTED = "UNSUPPORTED"
    ARCHIVE_SHADOW = "ARCHIVE_SHADOW"
    CLEAN_HISTORY_FALSE_EDIT = "CLEAN_HISTORY_FALSE_EDIT"
    CLEAN_HISTORY_FALSE_ARCHIVE = "CLEAN_HISTORY_FALSE_ARCHIVE"


_CALL_RATE_METRICS = (
    PilotRateMetricV1.EDIT,
    PilotRateMetricV1.ABSTAIN,
    PilotRateMetricV1.FALLBACK,
    PilotRateMetricV1.ERROR,
    PilotRateMetricV1.UNSUPPORTED,
    PilotRateMetricV1.ARCHIVE_SHADOW,
    PilotRateMetricV1.CLEAN_HISTORY_FALSE_EDIT,
    PilotRateMetricV1.CLEAN_HISTORY_FALSE_ARCHIVE,
)


class PilotTerminationReasonV1(StrEnum):
    ACTOR_FINISHED = "ACTOR_FINISHED"
    ACTOR_ENVIRONMENT_FAILURE = "ACTOR_ENVIRONMENT_FAILURE"
    ACTOR_UNKNOWN = "ACTOR_UNKNOWN"
    MAX_STEPS_EXHAUSTED = "MAX_STEPS_EXHAUSTED"
    UNKNOWN_MISSING_AUDIT_DETAIL = "UNKNOWN_MISSING_AUDIT_DETAIL"
    UNKNOWN_NONEXECUTED_ACTION = "UNKNOWN_NONEXECUTED_ACTION"


class PilotGroupKindV1(StrEnum):
    OVERALL = "OVERALL"
    HOST_ARM = "HOST_ARM"
    TASK = "TASK"


class PilotMatchedOutcomeV1(StrEnum):
    BOTH_SUCCESS = "BOTH_SUCCESS"
    BOTH_FAILURE = "BOTH_FAILURE"
    JOINT_IMPROVED = "JOINT_IMPROVED"
    JOINT_REGRESSED = "JOINT_REGRESSED"


@dataclass(frozen=True, slots=True)
class PilotClassificationResultV1:
    classification: PilotClassificationV1
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.classification) is not PilotClassificationV1:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "classification enum differs")
        _require_reason(self.reason_code, "classification reason")


@dataclass(frozen=True, slots=True)
class PilotRateSummaryV1:
    metric: PilotRateMetricV1
    unit: PilotRateUnitV1
    population_count: int
    measured_denominator: int
    positive_count: int | None
    missing_count: int
    not_applicable_count: int
    rate_ppm: int | None
    measurement_status: PilotMeasurementStatusV1
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.metric) is not PilotRateMetricV1 or type(self.unit) is not PilotRateUnitV1:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "rate enum differs")
        for name in (
            "population_count",
            "measured_denominator",
            "missing_count",
            "not_applicable_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise R25AnalysisContractError("INVALID_DENOMINATOR", f"{name} is invalid")
        if self.population_count != (
            self.measured_denominator + self.missing_count + self.not_applicable_count
        ):
            raise R25AnalysisContractError(
                "INVALID_DENOMINATOR", "population does not partition measured/missing/N-A"
            )
        if self.measured_denominator == 0:
            if self.positive_count is not None:
                raise R25AnalysisContractError(
                    "INVALID_DENOMINATOR", "unmeasurable rate cannot claim a zero numerator"
                )
        elif (
            type(self.positive_count) is not int
            or self.positive_count < 0
            or self.positive_count > self.measured_denominator
        ):
            raise R25AnalysisContractError("INVALID_DENOMINATOR", "numerator exceeds denominator")
        expected_rate = (
            None
            if self.measured_denominator == 0
            else (cast(int, self.positive_count) * 1_000_000 + self.measured_denominator // 2)
            // self.measured_denominator
        )
        if self.rate_ppm != expected_rate:
            raise R25AnalysisContractError("INVALID_RATE", "rate is not derived from exact counts")
        expected_status = _measurement_status(
            measured=self.measured_denominator,
            missing=self.missing_count,
            not_applicable=self.not_applicable_count,
        )
        if self.measurement_status is not expected_status:
            raise R25AnalysisContractError("INVALID_RATE", "measurement status differs")
        if (
            type(self.reason_codes) is not tuple
            or not self.reason_codes
            or tuple(sorted(set(self.reason_codes))) != self.reason_codes
        ):
            raise R25AnalysisContractError(
                "INVALID_REASON_CENSUS", "rate reasons must be sorted, unique, and non-empty"
            )
        for reason in self.reason_codes:
            _require_reason(reason, "rate reason")


@dataclass(frozen=True, slots=True)
class PilotStepSummaryV1:
    cell_denominator: int
    total_steps: int
    minimum_steps: int | None
    maximum_steps: int | None

    def __post_init__(self) -> None:
        if type(self.cell_denominator) is not int or self.cell_denominator < 0:
            raise R25AnalysisContractError("INVALID_DENOMINATOR", "step denominator is invalid")
        if type(self.total_steps) is not int or self.total_steps < 0:
            raise R25AnalysisContractError("INVALID_DENOMINATOR", "step total is invalid")
        if self.cell_denominator == 0:
            if self.total_steps or self.minimum_steps is not None or self.maximum_steps is not None:
                raise R25AnalysisContractError("INVALID_DENOMINATOR", "empty step summary differs")
        elif (
            type(self.minimum_steps) is not int
            or type(self.maximum_steps) is not int
            or self.minimum_steps < 1
            or self.maximum_steps < self.minimum_steps
            or not self.minimum_steps * self.cell_denominator
            <= self.total_steps
            <= self.maximum_steps * self.cell_denominator
        ):
            raise R25AnalysisContractError("INVALID_DENOMINATOR", "step range differs")


@dataclass(frozen=True, slots=True)
class PilotTokenSummaryV1:
    """Token totals over only calls with complete attempt-level accounting."""

    population_calls: int
    measured_call_denominator: int
    missing_call_count: int
    not_applicable_call_count: int
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    measurement_status: PilotMeasurementStatusV1

    def __post_init__(self) -> None:
        for name in (
            "population_calls",
            "measured_call_denominator",
            "missing_call_count",
            "not_applicable_call_count",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise R25AnalysisContractError("INVALID_TOKEN_CENSUS", f"{name} is invalid")
        if self.population_calls != (
            self.measured_call_denominator
            + self.missing_call_count
            + self.not_applicable_call_count
        ):
            raise R25AnalysisContractError(
                "INVALID_TOKEN_CENSUS", "token population partition differs"
            )
        token_values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.total_tokens,
        )
        if self.measured_call_denominator == 0:
            if any(item is not None for item in token_values):
                raise R25AnalysisContractError(
                    "INVALID_TOKEN_CENSUS", "unmeasured tokens cannot claim zero totals"
                )
        elif any(type(item) is not int or item < 0 for item in token_values):
            raise R25AnalysisContractError("INVALID_TOKEN_CENSUS", "token totals are absent")
        else:
            input_tokens, cached_tokens, output_tokens, total_tokens = cast(
                tuple[int, int, int, int], token_values
            )
            if total_tokens != input_tokens + output_tokens:
                raise R25AnalysisContractError("INVALID_TOKEN_CENSUS", "token totals differ")
            if cached_tokens > input_tokens:
                raise R25AnalysisContractError(
                    "INVALID_TOKEN_CENSUS", "cached input exceeds input tokens"
                )
        expected_status = _measurement_status(
            measured=self.measured_call_denominator,
            missing=self.missing_call_count,
            not_applicable=self.not_applicable_call_count,
        )
        if self.measurement_status is not expected_status:
            raise R25AnalysisContractError(
                "INVALID_TOKEN_CENSUS", "token measurement status differs"
            )


@dataclass(frozen=True, slots=True)
class PilotTerminationCountV1:
    reason: PilotTerminationReasonV1
    count: int

    def __post_init__(self) -> None:
        if type(self.reason) is not PilotTerminationReasonV1:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "termination enum differs")
        if type(self.count) is not int or self.count < 0:
            raise R25AnalysisContractError("INVALID_DENOMINATOR", "termination count is invalid")


@dataclass(frozen=True, slots=True)
class PilotCellAnalysisV1:
    sequence_index: int
    task_id: str
    host: PilotHostV1
    arm: PilotArmV1
    source_cell_sha256: str
    official_success: bool
    official_score_ppm: int
    steps: int
    executed_actions: int
    termination_reason: PilotTerminationReasonV1
    repeated_action: PilotClassificationResultV1
    unnecessary_action: PilotClassificationResultV1
    wrong_action: PilotClassificationResultV1
    wrong_edit: PilotClassificationResultV1
    premature_stop: PilotClassificationResultV1
    call_rates: tuple[PilotRateSummaryV1, ...]
    actor_provider_tokens: PilotTokenSummaryV1
    sentinel_openai_tokens: PilotTokenSummaryV1
    audit_detail_present_count: int
    audit_detail_missing_count: int
    openai_calls: int
    cost_usd_micros: int
    wall_time_ms: int

    def __post_init__(self) -> None:
        if type(self.sequence_index) is not int or self.sequence_index < 0:
            raise R25AnalysisContractError("INVALID_CELL", "cell index is invalid")
        _require_id(self.task_id, "task_id")
        if type(self.host) is not PilotHostV1 or type(self.arm) is not PilotArmV1:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "cell identity enum differs")
        _require_sha256(self.source_cell_sha256, "source_cell_sha256")
        if type(self.official_success) is not bool:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "official success is not bool")
        if (
            type(self.official_score_ppm) is not int
            or not 0 <= self.official_score_ppm <= 1_000_000
        ):
            raise R25AnalysisContractError("INVALID_OFFICIAL_RESULT", "official score is invalid")
        for name in (
            "steps",
            "executed_actions",
            "audit_detail_present_count",
            "audit_detail_missing_count",
            "openai_calls",
            "cost_usd_micros",
            "wall_time_ms",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise R25AnalysisContractError("INVALID_CELL", f"{name} is invalid")
        if self.steps < 1 or self.executed_actions > self.steps:
            raise R25AnalysisContractError("INVALID_CELL", "step/action census differs")
        if self.audit_detail_present_count + self.audit_detail_missing_count != self.steps:
            raise R25AnalysisContractError("INVALID_CELL", "audit-detail census differs")
        if type(self.termination_reason) is not PilotTerminationReasonV1:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "termination reason differs")
        for item in (
            self.repeated_action,
            self.unnecessary_action,
            self.wrong_action,
            self.wrong_edit,
            self.premature_stop,
        ):
            if type(item) is not PilotClassificationResultV1:
                raise R25AnalysisContractError("UNTRUSTED_TYPE", "cell classification differs")
        if tuple(item.metric for item in self.call_rates) != _CALL_RATE_METRICS:
            raise R25AnalysisContractError("INVALID_RATE_CENSUS", "cell call-rate census differs")
        if any(item.population_count != self.steps for item in self.call_rates):
            raise R25AnalysisContractError("INVALID_DENOMINATOR", "cell rate population differs")
        if (
            type(self.actor_provider_tokens) is not PilotTokenSummaryV1
            or type(self.sentinel_openai_tokens) is not PilotTokenSummaryV1
            or self.actor_provider_tokens.population_calls != self.steps
            or self.sentinel_openai_tokens.population_calls != self.steps
        ):
            raise R25AnalysisContractError("INVALID_TOKEN_CENSUS", "cell token census differs")


@dataclass(frozen=True, slots=True)
class PilotGroupAnalysisV1:
    kind: PilotGroupKindV1
    group_id: str
    host: PilotHostV1 | None
    arm: PilotArmV1 | None
    task_id: str | None
    cell_count: int
    official_success: PilotRateSummaryV1
    steps: PilotStepSummaryV1
    call_rates: tuple[PilotRateSummaryV1, ...]
    termination_counts: tuple[PilotTerminationCountV1, ...]
    actor_provider_tokens: PilotTokenSummaryV1
    sentinel_openai_tokens: PilotTokenSummaryV1
    openai_calls: int
    cost_usd_micros: int
    wall_time_ms: int

    def __post_init__(self) -> None:
        if type(self.kind) is not PilotGroupKindV1:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "group kind differs")
        _require_id(self.group_id, "group_id")
        if self.kind is PilotGroupKindV1.HOST_ARM:
            if (
                type(self.host) is not PilotHostV1
                or type(self.arm) is not PilotArmV1
                or self.task_id is not None
            ):
                raise R25AnalysisContractError("INVALID_GROUP", "host/arm group identity differs")
        elif self.kind is PilotGroupKindV1.TASK:
            if self.host is not None or self.arm is not None or self.task_id is None:
                raise R25AnalysisContractError("INVALID_GROUP", "task group identity differs")
            _require_id(self.task_id, "group task_id")
        elif self.host is not None or self.arm is not None or self.task_id is not None:
            raise R25AnalysisContractError("INVALID_GROUP", "overall group identity differs")
        for name in ("cell_count", "openai_calls", "cost_usd_micros", "wall_time_ms"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise R25AnalysisContractError("INVALID_GROUP", f"{name} is invalid")
        if self.cell_count < 1:
            raise R25AnalysisContractError("INVALID_GROUP", "group is empty")
        if (
            type(self.official_success) is not PilotRateSummaryV1
            or self.official_success.metric is not PilotRateMetricV1.OFFICIAL_SUCCESS
            or self.official_success.unit is not PilotRateUnitV1.CELL
            or self.official_success.population_count != self.cell_count
        ):
            raise R25AnalysisContractError("INVALID_GROUP", "official-success summary differs")
        if (
            type(self.steps) is not PilotStepSummaryV1
            or self.steps.cell_denominator != self.cell_count
        ):
            raise R25AnalysisContractError("INVALID_GROUP", "step summary differs")
        if tuple(item.metric for item in self.call_rates) != _CALL_RATE_METRICS:
            raise R25AnalysisContractError("INVALID_RATE_CENSUS", "group call-rate census differs")
        if tuple(item.reason for item in self.termination_counts) != tuple(
            PilotTerminationReasonV1
        ):
            raise R25AnalysisContractError(
                "INVALID_TERMINATION_CENSUS", "termination census differs"
            )
        if sum(item.count for item in self.termination_counts) != self.cell_count:
            raise R25AnalysisContractError(
                "INVALID_TERMINATION_CENSUS", "termination count differs"
            )
        call_population = self.call_rates[0].population_count
        if (
            type(self.actor_provider_tokens) is not PilotTokenSummaryV1
            or type(self.sentinel_openai_tokens) is not PilotTokenSummaryV1
            or self.actor_provider_tokens.population_calls != call_population
            or self.sentinel_openai_tokens.population_calls != call_population
        ):
            raise R25AnalysisContractError("INVALID_TOKEN_CENSUS", "group token census differs")


@dataclass(frozen=True, slots=True)
class PilotMatchedPairV1:
    task_id: str
    host: PilotHostV1
    baseline_sequence_index: int
    joint_sequence_index: int
    baseline_success: bool
    joint_success: bool
    outcome: PilotMatchedOutcomeV1
    baseline_steps: int
    joint_steps: int
    joint_minus_baseline_steps: int
    baseline_termination: PilotTerminationReasonV1
    joint_termination: PilotTerminationReasonV1
    termination_comparable: bool
    baseline_openai_calls: int
    joint_openai_calls: int
    baseline_cost_usd_micros: int
    joint_cost_usd_micros: int
    baseline_wall_time_ms: int
    joint_wall_time_ms: int
    baseline_actor_provider_tokens: PilotTokenSummaryV1
    joint_actor_provider_tokens: PilotTokenSummaryV1
    baseline_sentinel_openai_tokens: PilotTokenSummaryV1
    joint_sentinel_openai_tokens: PilotTokenSummaryV1

    def __post_init__(self) -> None:
        _require_id(self.task_id, "matched task_id")
        if type(self.host) is not PilotHostV1 or type(self.outcome) is not PilotMatchedOutcomeV1:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "matched-pair enum differs")
        for name in ("baseline_sequence_index", "joint_sequence_index"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise R25AnalysisContractError("INVALID_MATCHED_PAIR", f"{name} is invalid")
        for success in (
            cast(object, self.baseline_success),
            cast(object, self.joint_success),
        ):
            if type(success) is not bool:
                raise R25AnalysisContractError("UNTRUSTED_TYPE", "matched success flag differs")
        expected_outcome = (
            PilotMatchedOutcomeV1.BOTH_SUCCESS
            if self.baseline_success and self.joint_success
            else PilotMatchedOutcomeV1.BOTH_FAILURE
            if not self.baseline_success and not self.joint_success
            else PilotMatchedOutcomeV1.JOINT_IMPROVED
            if self.joint_success
            else PilotMatchedOutcomeV1.JOINT_REGRESSED
        )
        if self.outcome is not expected_outcome:
            raise R25AnalysisContractError("INVALID_MATCHED_PAIR", "matched outcome differs")
        if (
            type(self.baseline_steps) is not int
            or type(self.joint_steps) is not int
            or self.baseline_steps < 1
            or self.joint_steps < 1
            or self.joint_minus_baseline_steps != self.joint_steps - self.baseline_steps
        ):
            raise R25AnalysisContractError("INVALID_MATCHED_PAIR", "matched steps differ")
        if (
            type(self.baseline_termination) is not PilotTerminationReasonV1
            or type(self.joint_termination) is not PilotTerminationReasonV1
            or type(self.termination_comparable) is not bool
        ):
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "matched termination differs")
        unknown_reasons = {
            PilotTerminationReasonV1.UNKNOWN_MISSING_AUDIT_DETAIL,
            PilotTerminationReasonV1.UNKNOWN_NONEXECUTED_ACTION,
        }
        if self.termination_comparable != (
            self.baseline_termination not in unknown_reasons
            and self.joint_termination not in unknown_reasons
        ):
            raise R25AnalysisContractError(
                "INVALID_MATCHED_PAIR", "termination comparability differs"
            )
        for name in (
            "baseline_openai_calls",
            "joint_openai_calls",
            "baseline_cost_usd_micros",
            "joint_cost_usd_micros",
            "baseline_wall_time_ms",
            "joint_wall_time_ms",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise R25AnalysisContractError("INVALID_MATCHED_PAIR", f"{name} is invalid")
        for name in (
            "baseline_actor_provider_tokens",
            "joint_actor_provider_tokens",
            "baseline_sentinel_openai_tokens",
            "joint_sentinel_openai_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not PilotTokenSummaryV1 or value.population_calls < 1:
                raise R25AnalysisContractError(
                    "INVALID_MATCHED_PAIR", f"{name} token census differs"
                )
        if (
            self.baseline_actor_provider_tokens.population_calls != self.baseline_steps
            or self.baseline_sentinel_openai_tokens.population_calls != self.baseline_steps
            or self.joint_actor_provider_tokens.population_calls != self.joint_steps
            or self.joint_sentinel_openai_tokens.population_calls != self.joint_steps
        ):
            raise R25AnalysisContractError(
                "INVALID_MATCHED_PAIR", "matched token denominator differs from steps"
            )


@dataclass(frozen=True, slots=True)
class PilotMatchedComparisonV1:
    comparison_id: str
    host: PilotHostV1 | None
    pair_count: int
    both_success_count: int
    both_failure_count: int
    joint_improved_count: int
    joint_regressed_count: int
    baseline_success_count: int
    joint_success_count: int
    baseline_total_steps: int
    joint_total_steps: int
    joint_minus_baseline_total_steps: int
    comparable_termination_pairs: int
    same_termination_count: int
    different_termination_count: int
    missing_termination_pairs: int
    baseline_openai_calls: int
    joint_openai_calls: int
    baseline_cost_usd_micros: int
    joint_cost_usd_micros: int
    baseline_wall_time_ms: int
    joint_wall_time_ms: int
    baseline_actor_provider_tokens: PilotTokenSummaryV1
    joint_actor_provider_tokens: PilotTokenSummaryV1
    baseline_sentinel_openai_tokens: PilotTokenSummaryV1
    joint_sentinel_openai_tokens: PilotTokenSummaryV1

    def __post_init__(self) -> None:
        _require_id(self.comparison_id, "comparison_id")
        if self.host is not None and type(self.host) is not PilotHostV1:
            raise R25AnalysisContractError("UNTRUSTED_TYPE", "comparison host differs")
        for name in (
            "pair_count",
            "both_success_count",
            "both_failure_count",
            "joint_improved_count",
            "joint_regressed_count",
            "baseline_success_count",
            "joint_success_count",
            "baseline_total_steps",
            "joint_total_steps",
            "comparable_termination_pairs",
            "same_termination_count",
            "different_termination_count",
            "missing_termination_pairs",
            "baseline_openai_calls",
            "joint_openai_calls",
            "baseline_cost_usd_micros",
            "joint_cost_usd_micros",
            "baseline_wall_time_ms",
            "joint_wall_time_ms",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise R25AnalysisContractError("INVALID_COMPARISON", f"{name} is invalid")
        if type(self.joint_minus_baseline_total_steps) is not int or (
            self.joint_minus_baseline_total_steps
            != self.joint_total_steps - self.baseline_total_steps
        ):
            raise R25AnalysisContractError("INVALID_COMPARISON", "step delta differs")
        if self.pair_count != (
            self.both_success_count
            + self.both_failure_count
            + self.joint_improved_count
            + self.joint_regressed_count
        ):
            raise R25AnalysisContractError("INVALID_COMPARISON", "outcome denominator differs")
        if self.baseline_success_count != self.both_success_count + self.joint_regressed_count:
            raise R25AnalysisContractError("INVALID_COMPARISON", "baseline success count differs")
        if self.joint_success_count != self.both_success_count + self.joint_improved_count:
            raise R25AnalysisContractError("INVALID_COMPARISON", "joint success count differs")
        if self.pair_count != self.comparable_termination_pairs + self.missing_termination_pairs:
            raise R25AnalysisContractError("INVALID_COMPARISON", "termination denominator differs")
        if self.comparable_termination_pairs != (
            self.same_termination_count + self.different_termination_count
        ):
            raise R25AnalysisContractError("INVALID_COMPARISON", "termination counts differ")
        for name in (
            "baseline_actor_provider_tokens",
            "joint_actor_provider_tokens",
            "baseline_sentinel_openai_tokens",
            "joint_sentinel_openai_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not PilotTokenSummaryV1:
                raise R25AnalysisContractError("INVALID_COMPARISON", f"{name} token census differs")
        if (
            self.baseline_actor_provider_tokens.population_calls != self.baseline_total_steps
            or self.baseline_sentinel_openai_tokens.population_calls != self.baseline_total_steps
            or self.joint_actor_provider_tokens.population_calls != self.joint_total_steps
            or self.joint_sentinel_openai_tokens.population_calls != self.joint_total_steps
        ):
            raise R25AnalysisContractError(
                "INVALID_COMPARISON", "matched token denominator differs from steps"
            )


@dataclass(frozen=True, slots=True)
class PilotAnalysisV1:
    source_stage_evidence_sha256: str
    source_manifest_sha256: str
    manifest_sha256: str
    run_id: str
    cells: tuple[PilotCellAnalysisV1, ...]
    host_arm_groups: tuple[PilotGroupAnalysisV1, ...]
    task_groups: tuple[PilotGroupAnalysisV1, ...]
    matched_pairs: tuple[PilotMatchedPairV1, ...]
    matched_host_comparisons: tuple[PilotMatchedComparisonV1, ...]
    matched_overall: PilotMatchedComparisonV1
    overall: PilotGroupAnalysisV1
    limitations: tuple[str, ...]
    schema_version: str = PILOT_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PILOT_ANALYSIS_SCHEMA_VERSION:
            raise R25AnalysisContractError("UNKNOWN_SCHEMA", "analysis schema differs")
        for value, name in (
            (self.source_stage_evidence_sha256, "source_stage_evidence_sha256"),
            (self.source_manifest_sha256, "source_manifest_sha256"),
            (self.manifest_sha256, "manifest_sha256"),
        ):
            _require_sha256(value, name)
        _require_id(self.run_id, "run_id")
        if type(self.cells) is not tuple or not 80 <= len(self.cells) <= 120:
            raise R25AnalysisContractError("INVALID_CELL_MATRIX", "analysis cell matrix differs")
        if tuple(item.sequence_index for item in self.cells) != tuple(range(len(self.cells))):
            raise R25AnalysisContractError("INVALID_CELL_MATRIX", "analysis cell order differs")
        if (
            type(self.host_arm_groups) is not tuple
            or len(self.host_arm_groups) != 4
            or any(item.kind is not PilotGroupKindV1.HOST_ARM for item in self.host_arm_groups)
        ):
            raise R25AnalysisContractError("INVALID_GROUP", "host/arm group census differs")
        task_count = len({item.task_id for item in self.cells})
        if (
            type(self.task_groups) is not tuple
            or len(self.task_groups) != task_count
            or any(item.kind is not PilotGroupKindV1.TASK for item in self.task_groups)
        ):
            raise R25AnalysisContractError("INVALID_GROUP", "task group census differs")
        if (
            type(self.overall) is not PilotGroupAnalysisV1
            or self.overall.kind is not PilotGroupKindV1.OVERALL
        ):
            raise R25AnalysisContractError("INVALID_GROUP", "overall group differs")
        if self.overall.cell_count != len(self.cells):
            raise R25AnalysisContractError("INVALID_GROUP", "overall denominator differs")
        expected_pairs = len(self.cells) // 2
        if type(self.matched_pairs) is not tuple or len(self.matched_pairs) != expected_pairs:
            raise R25AnalysisContractError("INVALID_MATCHED_PAIR", "matched-pair census differs")
        if (
            type(self.matched_host_comparisons) is not tuple
            or len(self.matched_host_comparisons) != 2
            or any(item.host is None for item in self.matched_host_comparisons)
        ):
            raise R25AnalysisContractError(
                "INVALID_COMPARISON", "matched host comparison census differs"
            )
        if type(self.matched_overall) is not PilotMatchedComparisonV1 or (
            self.matched_overall.host is not None
            or self.matched_overall.pair_count != expected_pairs
        ):
            raise R25AnalysisContractError("INVALID_COMPARISON", "matched overall differs")
        if (
            type(self.limitations) is not tuple
            or not self.limitations
            or tuple(sorted(set(self.limitations))) != self.limitations
        ):
            raise R25AnalysisContractError("INVALID_LIMITATIONS", "limitations differ")
        for item in self.limitations:
            _require_reason(item, "limitation")


@dataclass(frozen=True, slots=True)
class _DecisionFacts:
    detail_present: bool
    pre_provider_status: str | None
    semantic_applicable: bool | None
    action_type: str | None
    abstain: bool | None
    fallback: bool | None
    error: bool | None
    unsupported: bool | None
    archive_shadow: bool | None
    actor_tokens: tuple[int, int, int, int] | None
    sentinel_tokens: tuple[int, int, int, int] | None | PilotMeasurementStatusV1


def _require_reason(value: object, name: str) -> str:
    if type(value) is not str or _REASON.fullmatch(value) is None:
        raise R25AnalysisContractError("INVALID_REASON", f"{name} is invalid")
    return value


def _require_id(value: object, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise R25AnalysisContractError("INVALID_ID", f"{name} is invalid")
    return value


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise R25AnalysisContractError("INVALID_SHA256", f"{name} is invalid")
    return value


def _object(value: object, name: str) -> dict[str, JsonValue]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise R25AnalysisContractError("UNTRUSTED_TYPE", f"{name} must be an exact object")
    return cast(dict[str, JsonValue], value)


def _array(value: object, name: str) -> list[JsonValue]:
    if type(value) is not list:
        raise R25AnalysisContractError("UNTRUSTED_TYPE", f"{name} must be an exact array")
    return cast(list[JsonValue], value)


def _int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise R25AnalysisContractError("INVALID_CENSUS", f"{name} is invalid")
    return value


def _measurement_status(
    *, measured: int, missing: int, not_applicable: int
) -> PilotMeasurementStatusV1:
    if measured:
        return PilotMeasurementStatusV1.PARTIAL if missing else PilotMeasurementStatusV1.EXACT
    if missing:
        return PilotMeasurementStatusV1.NOT_MEASURABLE
    if not_applicable:
        return PilotMeasurementStatusV1.NOT_APPLICABLE
    return PilotMeasurementStatusV1.NOT_MEASURABLE


def _rate(
    metric: PilotRateMetricV1,
    unit: PilotRateUnitV1,
    values: tuple[bool | None | PilotMeasurementStatusV1, ...],
    reasons: tuple[str, ...],
) -> PilotRateSummaryV1:
    measured = sum(type(item) is bool for item in values)
    positive = sum(item is True for item in values)
    missing = sum(item is None for item in values)
    not_applicable = sum(item is PilotMeasurementStatusV1.NOT_APPLICABLE for item in values)
    status = _measurement_status(measured=measured, missing=missing, not_applicable=not_applicable)
    return PilotRateSummaryV1(
        metric=metric,
        unit=unit,
        population_count=len(values),
        measured_denominator=measured,
        positive_count=None if measured == 0 else positive,
        missing_count=missing,
        not_applicable_count=not_applicable,
        rate_ppm=(None if measured == 0 else (positive * 1_000_000 + measured // 2) // measured),
        measurement_status=status,
        reason_codes=tuple(sorted(set(reasons))),
    )


def _census(value: object, name: str) -> dict[str, int]:
    mapping = _object(value, name)
    if set(mapping) != set(_CENSUS_FIELDS):
        raise R25AnalysisContractError("INVALID_FIELDS", f"{name} fields differ")
    result = {field: _int(mapping[field], f"{name}.{field}") for field in _CENSUS_FIELDS}
    if result["openai_calls"] != (
        result["rubric_openai_calls"] + result["history_policy_openai_calls"]
    ):
        raise R25AnalysisContractError("INVALID_CENSUS", f"{name} OpenAI roles differ")
    if result["actor_actions"] > result["actor_calls"]:
        raise R25AnalysisContractError("INVALID_CENSUS", f"{name} actions exceed calls")
    return result


def _sum_censuses(values: tuple[dict[str, int], ...]) -> dict[str, int]:
    return {field: sum(value[field] for value in values) for field in _CENSUS_FIELDS}


def _stage_projection(
    evidence: PilotStageEvidenceV1 | JsonValue,
) -> dict[str, JsonValue]:
    if type(evidence) is PilotStageEvidenceV1:
        value: JsonValue = cast(JsonValue, pilot_stage_evidence_projection(evidence))
    elif type(evidence) is dict:
        try:
            value = snapshot_json_value(evidence)
        except (TypeError, ValueError, RecursionError) as exc:
            raise R25AnalysisContractError(
                "NONCANONICAL_SOURCE", "stage projection is not bounded canonical JSON"
            ) from exc
    else:
        raise R25AnalysisContractError(
            "UNTRUSTED_TYPE", "stage evidence must be exact typed evidence or its projection"
        )
    return _object(value, "stage evidence")


def _validate_decision(
    value: JsonValue,
    *,
    expected_index: int,
) -> tuple[dict[str, JsonValue], dict[str, int]]:
    decision = _object(value, "decision")
    if set(decision) != _DECISION_FIELDS:
        raise R25AnalysisContractError("INVALID_FIELDS", "decision fields differ")
    if decision["actor_call_index"] != expected_index:
        raise R25AnalysisContractError("INVALID_DECISION_ORDER", "actor call order differs")
    _require_id(decision["logical_call_id"], "logical_call_id")
    nullable = {
        "case_execution_lease_sha256",
        "executed_action_sha256",
        "fallback_check",
        "fallback_reason",
        "history_policy_attempt_receipt_sha256",
        "live_policy_authority_sha256",
    }
    for field in _DECISION_FIELDS - {
        "actor_call_index",
        "census",
        "fallback_check",
        "fallback_reason",
        "pre_provider_outcome",
        "pre_provider_status",
        "rubric_attempt_receipt_sha256s",
    }:
        item = decision[field]
        if field in nullable and item is None:
            continue
        if field == "logical_call_id":
            continue
        _require_sha256(item, field)
    status = decision["pre_provider_status"]
    outcome = decision["pre_provider_outcome"]
    fallback_reason = decision["fallback_reason"]
    fallback_check = decision["fallback_check"]
    allowed_outcomes = {
        "OFF": {"OFF"},
        "READY": {"READY"},
        "BYPASSED_ORIGINAL": {"BYPASSED_ORIGINAL"},
        "FALLBACK_ORIGINAL": {
            "GENERIC_FALLBACK_ORIGINAL",
            "NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL",
        },
    }
    if (
        type(status) is not str
        or type(outcome) is not str
        or (outcome not in allowed_outcomes.get(status, set()))
    ):
        raise R25AnalysisContractError(
            "INVALID_PRE_PROVIDER_OUTCOME", "decision pre-provider outcome/status differ"
        )
    if status in {"OFF", "READY"}:
        invalid_fallback = fallback_reason is not None or fallback_check is not None
    elif status == "FALLBACK_ORIGINAL":
        invalid_fallback = type(fallback_reason) is not str or type(fallback_check) is not str
    else:
        invalid_fallback = fallback_reason is not None or type(fallback_check) is not str
    if invalid_fallback:
        raise R25AnalysisContractError(
            "INVALID_PRE_PROVIDER_OUTCOME", "decision fallback classification differs"
        )
    rubric_receipts = _array(decision["rubric_attempt_receipt_sha256s"], "rubric receipts")
    for item in rubric_receipts:
        _require_sha256(item, "rubric attempt receipt")
    census = _census(decision["census"], "decision.census")
    if census["actor_calls"] != 1 or census["actor_actions"] not in {0, 1}:
        raise R25AnalysisContractError("INVALID_CENSUS", "one decision must bind one actor call")
    if decision["provider_request_sha256"] != decision["final_request_sha256"]:
        raise R25AnalysisContractError(
            "PROVIDER_FINAL_REQUEST_MISMATCH", "provider request differs from final request"
        )
    if (decision["executed_action_sha256"] is not None) != (census["actor_actions"] == 1):
        raise R25AnalysisContractError("INVALID_CENSUS", "executed action census differs")
    if decision["executed_action_sha256"] is not None and (
        decision["executed_action_sha256"] != decision["parsed_action_sha256"]
    ):
        raise R25AnalysisContractError("ACTION_BINDING_MISMATCH", "executed action differs")
    if len(rubric_receipts) != census["rubric_openai_calls"]:
        raise R25AnalysisContractError("INVALID_CENSUS", "rubric attempt census differs")
    return decision, census


def _validate_stage(
    manifest: FrozenPilotManifestV1,
    source: dict[str, JsonValue],
) -> tuple[tuple[dict[str, JsonValue], ...], dict[str, int]]:
    if set(source) != _STAGE_FIELDS:
        raise R25AnalysisContractError("INVALID_FIELDS", "stage evidence fields differ")
    if source["schema_version"] != PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION:
        raise R25AnalysisContractError("UNKNOWN_SCHEMA", "driver evidence schema differs")
    _require_sha256(source["manifest_sha256"], "manifest_sha256")
    _require_id(source["run_id"], "run_id")
    for field in (
        "actor_resources_sha256",
        "history_policy_stage_sha256",
        "pilot_manifest_sha256",
    ):
        _require_sha256(source[field], field)
    if source["pilot_manifest_sha256"] != frozen_pilot_manifest_sha256(manifest):
        raise R25AnalysisContractError(
            "MANIFEST_BINDING_MISMATCH", "stage evidence binds another frozen pilot"
        )
    raw_cells = _array(source["cells"], "stage cells")
    expected_cells = manifest.cells
    if len(raw_cells) != len(expected_cells):
        raise R25AnalysisContractError(
            "INCOMPLETE_CELL_MATRIX", "every expected cell must remain in the denominator"
        )
    cells: list[dict[str, JsonValue]] = []
    cell_censuses: list[dict[str, int]] = []
    logical_ids: set[str] = set()
    reset_states: dict[str, str] = {}
    actor_resources: dict[str, str] = {}
    for index, (raw_cell, expected) in enumerate(zip(raw_cells, expected_cells, strict=True)):
        cell = _object(raw_cell, f"cell[{index}]")
        if set(cell) != _CELL_FIELDS:
            raise R25AnalysisContractError("INVALID_FIELDS", f"cell[{index}] fields differ")
        expected_identity = (
            index,
            expected.task_id,
            expected.task_parameters_sha256,
            expected.reset_seed,
            expected.host.value,
            expected.arm.value,
            expected.sentinel_mode,
        )
        actual_identity = (
            cell["sequence_index"],
            cell["task_id"],
            cell["task_parameters_sha256"],
            cell["reset_seed"],
            cell["host"],
            cell["arm"],
            cell["sentinel_mode"],
        )
        if actual_identity != expected_identity:
            raise R25AnalysisContractError(
                "CELL_MATRIX_MISMATCH", "cell order or matched task binding differs"
            )
        if (
            cell["manifest_sha256"] != source["manifest_sha256"]
            or cell["run_id"] != source["run_id"]
        ):
            raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", "cell run binding differs")
        if cell["history_policy_stage_sha256"] != source["history_policy_stage_sha256"]:
            raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", "cell policy stage differs")
        for field in (
            "actor_resource_sha256",
            "cleanup_receipt_sha256",
            "effective_reset_state_sha256",
            "history_policy_stage_sha256",
            "reset_receipt_sha256",
            "task_parameters_sha256",
        ):
            _require_sha256(cell[field], f"cell.{field}")
        prior_reset = reset_states.setdefault(
            expected.task_id, cast(str, cell["effective_reset_state_sha256"])
        )
        if prior_reset != cell["effective_reset_state_sha256"]:
            raise R25AnalysisContractError(
                "MATCHED_RESET_STATE_MISMATCH", "matched cells have different reset states"
            )
        prior_resource = actor_resources.setdefault(
            expected.host.value, cast(str, cell["actor_resource_sha256"])
        )
        if prior_resource != cell["actor_resource_sha256"]:
            raise R25AnalysisContractError(
                "ACTOR_RESOURCE_MISMATCH", "one host changed actor resources across cells"
            )
        raw_decisions = _array(cell["decisions"], "cell decisions")
        if not raw_decisions or len(raw_decisions) > manifest.max_steps_per_cell:
            raise R25AnalysisContractError("INVALID_STEP_CENSUS", "cell steps exceed pilot bound")
        decision_censuses: list[dict[str, int]] = []
        for call_index, raw_decision in enumerate(raw_decisions, 1):
            decision, decision_census = _validate_decision(raw_decision, expected_index=call_index)
            logical_call_id = cast(str, decision["logical_call_id"])
            if logical_call_id in logical_ids:
                raise R25AnalysisContractError(
                    "DUPLICATE_LOGICAL_CALL", "logical call repeats across the pilot"
                )
            logical_ids.add(logical_call_id)
            decision_censuses.append(decision_census)
        summed = _sum_censuses(tuple(decision_censuses))
        cell_census = _census(cell["census"], "cell.census")
        for field in _CENSUS_FIELDS:
            if field == "wall_time_ms":
                if cell_census[field] < summed[field]:
                    raise R25AnalysisContractError(
                        "INVALID_CENSUS", "cell wall time is below decision time"
                    )
            elif cell_census[field] != summed[field]:
                raise R25AnalysisContractError("INVALID_CENSUS", "cell census differs")
        official = _object(cell["official_result"], "official result")
        if set(official) != _OFFICIAL_FIELDS:
            raise R25AnalysisContractError("INVALID_FIELDS", "official result fields differ")
        if (
            official["task_id"] != expected.task_id
            or official["evaluator_id"] != OFFICIAL_RESULT_EVALUATOR_ID_V1
        ):
            raise R25AnalysisContractError(
                "INVALID_OFFICIAL_RESULT", "official evaluator/task binding differs"
            )
        if type(official["successful"]) is not bool:
            raise R25AnalysisContractError("INVALID_OFFICIAL_RESULT", "success is not bool")
        score = official["score_ppm"]
        if type(score) is not int or not 0 <= score <= 1_000_000:
            raise R25AnalysisContractError("INVALID_OFFICIAL_RESULT", "score is invalid")
        _require_sha256(official["reason_sha256"], "official reason")
        _require_sha256(official["result_payload_sha256"], "official payload")
        cells.append(cell)
        cell_censuses.append(cell_census)
    stage_census = _census(source["census"], "stage.census")
    if stage_census != _sum_censuses(tuple(cell_censuses)):
        raise R25AnalysisContractError("INVALID_CENSUS", "stage census differs from all cells")
    return tuple(cells), stage_census


def _tokens_from_attempts(
    attempts: list[JsonValue],
    *,
    cached_field: bool,
    name: str,
) -> tuple[int, int, int, int] | None:
    totals = [0, 0, 0, 0]
    for raw_attempt in attempts:
        attempt = _object(raw_attempt, name)
        input_tokens = attempt.get("input_tokens")
        output_tokens = attempt.get("output_tokens")
        total_tokens = attempt.get("total_tokens")
        cached_tokens = attempt.get("cached_input_tokens") if cached_field else 0
        token_values = (input_tokens, cached_tokens, output_tokens, total_tokens)
        if any(value is None for value in token_values):
            return None
        if any(type(value) is not int or value < 0 for value in token_values):
            raise R25AnalysisContractError("INVALID_TOKEN_CENSUS", f"{name} tokens differ")
        input_count, cached_count, output_count, total_count = cast(
            tuple[int, int, int, int], token_values
        )
        if total_count != input_count + output_count or cached_count > input_count:
            raise R25AnalysisContractError("INVALID_TOKEN_CENSUS", f"{name} totals differ")
        for index, count in enumerate(token_values):
            totals[index] += cast(int, count)
    return cast(tuple[int, int, int, int], tuple(totals))


def _token_summary(
    values: tuple[
        tuple[int, int, int, int] | None | PilotMeasurementStatusV1,
        ...,
    ],
) -> PilotTokenSummaryV1:
    measured = 0
    totals = [0, 0, 0, 0]
    for item in values:
        if type(item) is tuple:
            measured += 1
            for index, count in enumerate(item):
                totals[index] += count
    missing = sum(item is None for item in values)
    not_applicable = sum(item is PilotMeasurementStatusV1.NOT_APPLICABLE for item in values)
    return PilotTokenSummaryV1(
        population_calls=len(values),
        measured_call_denominator=measured,
        missing_call_count=missing,
        not_applicable_call_count=not_applicable,
        input_tokens=None if measured == 0 else totals[0],
        cached_input_tokens=None if measured == 0 else totals[1],
        output_tokens=None if measured == 0 else totals[2],
        total_tokens=None if measured == 0 else totals[3],
        measurement_status=_measurement_status(
            measured=measured, missing=missing, not_applicable=not_applicable
        ),
    )


def _merge_tokens(
    values: tuple[PilotTokenSummaryV1, ...],
) -> PilotTokenSummaryV1:
    measured = sum(item.measured_call_denominator for item in values)
    missing = sum(item.missing_call_count for item in values)
    not_applicable = sum(item.not_applicable_call_count for item in values)
    return PilotTokenSummaryV1(
        population_calls=sum(item.population_calls for item in values),
        measured_call_denominator=measured,
        missing_call_count=missing,
        not_applicable_call_count=not_applicable,
        input_tokens=(None if measured == 0 else sum(item.input_tokens or 0 for item in values)),
        cached_input_tokens=(
            None if measured == 0 else sum(item.cached_input_tokens or 0 for item in values)
        ),
        output_tokens=(None if measured == 0 else sum(item.output_tokens or 0 for item in values)),
        total_tokens=(None if measured == 0 else sum(item.total_tokens or 0 for item in values)),
        measurement_status=_measurement_status(
            measured=measured, missing=missing, not_applicable=not_applicable
        ),
    )


def _detail_facts(
    decision: dict[str, JsonValue],
    audit_details: dict[str, JsonValue],
) -> _DecisionFacts:
    detail_hash = cast(str, decision["runtime_audit_detail_sha256"])
    raw_detail = audit_details.get(detail_hash)
    if raw_detail is None:
        return _DecisionFacts(False, None, None, None, None, None, None, None, None, None, None)
    try:
        detail_value = snapshot_json_value(raw_detail)
    except (TypeError, ValueError, RecursionError) as exc:
        raise R25AnalysisContractError(
            "INVALID_AUDIT_DETAIL", "audit detail is not bounded canonical JSON"
        ) from exc
    if canonical_sha256(detail_value) != detail_hash:
        raise R25AnalysisContractError("AUDIT_DETAIL_HASH_MISMATCH", "audit detail hash differs")
    detail = _object(detail_value, "audit detail")
    if detail.get("schema_version") != PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION:
        raise R25AnalysisContractError("UNKNOWN_SCHEMA", "audit detail schema differs")
    if detail.get("logical_call_id") != decision["logical_call_id"]:
        raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", "audit logical call differs")
    pre = _object(detail.get("pre_provider"), "audit pre_provider")
    if detail.get("pre_provider_sha256") != canonical_sha256(cast(JsonValue, pre)):
        raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", "pre-provider hash differs")
    for field in ("raw_request_sha256", "final_request_sha256", "exact_diff_sha256"):
        if pre.get(field) != decision[field]:
            raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", f"audit {field} differs")
    for field in (
        "status",
        "outcome",
        "fallback_reason",
        "fallback_check",
    ):
        decision_field = f"pre_provider_{field}" if field in {"status", "outcome"} else field
        if pre.get(field) != decision[decision_field]:
            raise R25AnalysisContractError(
                "TRACE_BINDING_MISMATCH", f"audit pre-provider {field} differs"
            )
    terminal = _object(detail.get("terminal"), "audit terminal")
    if terminal.get("successful_provider_response_sha256") != decision["provider_response_sha256"]:
        raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", "provider response differs")
    if terminal.get("parsed_action_sha256") != decision["parsed_action_sha256"]:
        raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", "parsed action differs")
    if terminal.get("executed_action_sha256") != decision["executed_action_sha256"]:
        raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", "executed action differs")
    parsed_action = terminal.get("parsed_action")
    if canonical_sha256(parsed_action) != decision["parsed_action_sha256"]:
        raise R25AnalysisContractError("TRACE_BINDING_MISMATCH", "parsed action content differs")
    action = _object(parsed_action, "parsed action")
    action_type = action.get("action_type")
    if type(action_type) is not str:
        raise R25AnalysisContractError("INVALID_ACTION", "action_type is absent")
    action_executed = terminal.get("action_executed")
    if type(action_executed) is not bool or action_executed != (
        decision["executed_action_sha256"] is not None
    ):
        raise R25AnalysisContractError("ACTION_BINDING_MISMATCH", "action execution differs")
    attempts = _array(detail.get("actor_provider_attempts"), "actor provider attempts")
    if not attempts:
        raise R25AnalysisContractError("INVALID_AUDIT_DETAIL", "provider attempt census is empty")
    provider_failure = False
    provider_success = False
    for attempt_value in attempts:
        attempt = _object(attempt_value, "actor provider attempt")
        status = attempt.get("status")
        if status == "FAILED":
            provider_failure = True
        elif status == "SUCCEEDED":
            provider_success = True
        else:
            raise R25AnalysisContractError("INVALID_AUDIT_DETAIL", "provider status is unknown")
    if not provider_success:
        raise R25AnalysisContractError("INVALID_AUDIT_DETAIL", "terminal detail has no success")
    actor_tokens = _tokens_from_attempts(
        attempts, cached_field=False, name="actor provider attempt"
    )
    status = pre.get("status")
    if status not in {"OFF", "READY", "FALLBACK_ORIGINAL", "BYPASSED_ORIGINAL"}:
        raise R25AnalysisContractError("INVALID_AUDIT_DETAIL", "pre-provider status is unknown")
    fallback = status == "FALLBACK_ORIGINAL"
    fallback_reason = pre.get("fallback_reason")
    if fallback and type(fallback_reason) is not str:
        raise R25AnalysisContractError("INVALID_AUDIT_DETAIL", "fallback reason is absent")
    unsupported = fallback and fallback_reason == "UNSUPPORTED_HISTORY_FAMILY"
    technical_fallback = fallback and not unsupported
    restricted = _object(pre.get("restricted_stage_projection"), "restricted projection")
    abstain = False
    archive = False
    if status == "READY":
        vertical = _object(restricted.get("vertical_output"), "vertical output")
        raw_policy_decisions = _array(vertical.get("decisions"), "vertical decisions")
        operations: list[str] = []
        for raw_policy_decision in raw_policy_decisions:
            operation = _object(raw_policy_decision, "vertical decision").get("operation")
            if operation not in {"KEEP", "KEEP_UNCERTAIN", "DROP", "REPLACE"}:
                raise R25AnalysisContractError(
                    "INVALID_AUDIT_DETAIL", "vertical operation is unknown"
                )
            operations.append(cast(str, operation))
        abstain = "KEEP_UNCERTAIN" in operations
        relevance = _object(restricted.get("path_relevance_output"), "path relevance")
        records = _array(relevance.get("records"), "path relevance records")
        for raw_record in records:
            disposition = _object(raw_record, "path relevance record").get("disposition")
            if disposition not in {"RETAIN", "ARCHIVE_SHADOW"}:
                raise R25AnalysisContractError(
                    "INVALID_AUDIT_DETAIL", "relevance disposition is unknown"
                )
            archive = archive or disposition == "ARCHIVE_SHADOW"
    if status == "OFF":
        sentinel_tokens: tuple[int, int, int, int] | None | PilotMeasurementStatusV1 = (
            PilotMeasurementStatusV1.NOT_APPLICABLE
        )
    elif status == "BYPASSED_ORIGINAL":
        sentinel_tokens = (0, 0, 0, 0)
    else:
        raw_live_attempts = restricted.get("live_attempt_receipts")
        if raw_live_attempts is None:
            sentinel_tokens = None
        else:
            live_attempts = _array(raw_live_attempts, "Sentinel OpenAI attempts")
            decision_census = _census(decision["census"], "decision.census")
            if len(live_attempts) != decision_census["openai_calls"]:
                raise R25AnalysisContractError(
                    "INVALID_TOKEN_CENSUS", "Sentinel attempt/token census differs"
                )
            sentinel_tokens = _tokens_from_attempts(
                live_attempts,
                cached_field=True,
                name="Sentinel OpenAI attempt",
            )
    return _DecisionFacts(
        detail_present=True,
        pre_provider_status=cast(str, status),
        semantic_applicable=status in {"READY", "FALLBACK_ORIGINAL"},
        action_type=action_type,
        abstain=abstain,
        fallback=fallback,
        error=provider_failure or technical_fallback or action_type in {"error_env", "unknown"},
        unsupported=unsupported,
        archive_shadow=archive,
        actor_tokens=actor_tokens,
        sentinel_tokens=sentinel_tokens,
    )


def _classification(
    classification: PilotClassificationV1, reason: str
) -> PilotClassificationResultV1:
    return PilotClassificationResultV1(classification, reason)


def _cell_analysis(
    cell: dict[str, JsonValue],
    *,
    audit_details: dict[str, JsonValue],
    max_steps: int,
) -> PilotCellAnalysisV1:
    decisions = tuple(
        _object(item, "decision") for item in _array(cell["decisions"], "cell decisions")
    )
    facts = tuple(_detail_facts(item, audit_details) for item in decisions)
    official = _object(cell["official_result"], "official result")
    arm = PilotArmV1(cast(str, cell["arm"]))
    if any(
        fact.detail_present
        and (
            (arm is PilotArmV1.BASELINE and fact.pre_provider_status != "OFF")
            or (arm is PilotArmV1.JOINT_SENTINEL and fact.pre_provider_status == "OFF")
        )
        for fact in facts
    ):
        raise R25AnalysisContractError(
            "ARM_MODE_MISMATCH", "audit pre-provider status differs from pilot arm"
        )
    executed_hashes = tuple(
        cast(str, item["executed_action_sha256"])
        for item in decisions
        if item["executed_action_sha256"] is not None
    )
    repeated = any(left == right for left, right in zip(executed_hashes, executed_hashes[1:]))
    repeated_classification = _classification(
        PilotClassificationV1.OBSERVED if repeated else PilotClassificationV1.NOT_OBSERVED,
        "EXACT_CONSECUTIVE_EXECUTED_ACTION_HASH_REPEAT"
        if repeated
        else "NO_EXACT_CONSECUTIVE_EXECUTED_ACTION_HASH_REPEAT",
    )
    edit_values: tuple[bool | None | PilotMeasurementStatusV1, ...] = tuple(
        item["raw_request_sha256"] != item["final_request_sha256"] for item in decisions
    )
    abstain_values: tuple[bool | None | PilotMeasurementStatusV1, ...]
    fallback_values: tuple[bool | None | PilotMeasurementStatusV1, ...]
    unsupported_values: tuple[bool | None | PilotMeasurementStatusV1, ...]
    archive_values: tuple[bool | None | PilotMeasurementStatusV1, ...]
    clean_false_edit_values: tuple[bool | None | PilotMeasurementStatusV1, ...]
    clean_false_archive_values: tuple[bool | None | PilotMeasurementStatusV1, ...]
    if arm is PilotArmV1.BASELINE:
        semantic_na = tuple(PilotMeasurementStatusV1.NOT_APPLICABLE for _ in decisions)
        abstain_values = fallback_values = unsupported_values = archive_values = semantic_na
        clean_false_edit_values = clean_false_archive_values = semantic_na
    else:

        def semantic_value(
            value: bool | None, fact: _DecisionFacts
        ) -> bool | None | PilotMeasurementStatusV1:
            if not fact.detail_present:
                return None
            if fact.semantic_applicable is False:
                return PilotMeasurementStatusV1.NOT_APPLICABLE
            return value

        abstain_values = tuple(semantic_value(item.abstain, item) for item in facts)
        fallback_values = tuple(semantic_value(item.fallback, item) for item in facts)
        unsupported_values = tuple(semantic_value(item.unsupported, item) for item in facts)
        archive_values = tuple(semantic_value(item.archive_shadow, item) for item in facts)
        # The source has no independent clean-history label.  Do not let the
        # same policy/rubric output declare its own false-positive denominator.
        clean_false_edit_values = tuple(None for _ in decisions)
        clean_false_archive_values = tuple(None for _ in decisions)
    error_values = tuple(item.error if item.detail_present else None for item in facts)
    rates = (
        _rate(
            PilotRateMetricV1.EDIT,
            PilotRateUnitV1.ACTOR_CALL,
            edit_values,
            ("RAW_FINAL_REQUEST_HASH_COMPARISON",),
        ),
        _rate(
            PilotRateMetricV1.ABSTAIN,
            PilotRateUnitV1.ACTOR_CALL,
            abstain_values,
            (
                "KEEP_UNCERTAIN_DECISION_PRESENT"
                if arm is PilotArmV1.JOINT_SENTINEL
                else "SENTINEL_DISABLED_BASELINE",
            ),
        ),
        _rate(
            PilotRateMetricV1.FALLBACK,
            PilotRateUnitV1.ACTOR_CALL,
            fallback_values,
            (
                "PRE_PROVIDER_FALLBACK_ORIGINAL"
                if arm is PilotArmV1.JOINT_SENTINEL
                else "SENTINEL_DISABLED_BASELINE",
            ),
        ),
        _rate(
            PilotRateMetricV1.ERROR,
            PilotRateUnitV1.ACTOR_CALL,
            error_values,
            ("TECHNICAL_FALLBACK_FAILED_PROVIDER_OR_ACTOR_ERROR_SIGNAL",),
        ),
        _rate(
            PilotRateMetricV1.UNSUPPORTED,
            PilotRateUnitV1.ACTOR_CALL,
            unsupported_values,
            (
                "UNSUPPORTED_HISTORY_FAMILY_FALLBACK"
                if arm is PilotArmV1.JOINT_SENTINEL
                else "SENTINEL_DISABLED_BASELINE",
            ),
        ),
        _rate(
            PilotRateMetricV1.ARCHIVE_SHADOW,
            PilotRateUnitV1.ACTOR_CALL,
            archive_values,
            (
                "PATH_RELEVANCE_ARCHIVE_SHADOW_DISPOSITION"
                if arm is PilotArmV1.JOINT_SENTINEL
                else "SENTINEL_DISABLED_BASELINE",
            ),
        ),
        _rate(
            PilotRateMetricV1.CLEAN_HISTORY_FALSE_EDIT,
            PilotRateUnitV1.ACTOR_CALL,
            clean_false_edit_values,
            (
                "NO_INDEPENDENT_CLEAN_HISTORY_LABEL"
                if arm is PilotArmV1.JOINT_SENTINEL
                else "SENTINEL_DISABLED_BASELINE",
            ),
        ),
        _rate(
            PilotRateMetricV1.CLEAN_HISTORY_FALSE_ARCHIVE,
            PilotRateUnitV1.ACTOR_CALL,
            clean_false_archive_values,
            (
                "NO_INDEPENDENT_CLEAN_HISTORY_LABEL"
                if arm is PilotArmV1.JOINT_SENTINEL
                else "SENTINEL_DISABLED_BASELINE",
            ),
        ),
    )
    any_edit = any(item is True for item in edit_values)
    wrong_edit = _classification(
        PilotClassificationV1.NOT_MEASURABLE if any_edit else PilotClassificationV1.NOT_APPLICABLE,
        "NO_INDEPENDENT_EDIT_CORRECTNESS_LABEL" if any_edit else "NO_EDIT_APPLIED",
    )
    last_fact = facts[-1]
    last_executed = decisions[-1]["executed_action_sha256"] is not None
    if last_fact.detail_present and last_fact.action_type == "finished":
        termination = PilotTerminationReasonV1.ACTOR_FINISHED
        premature = _classification(
            PilotClassificationV1.NOT_OBSERVED
            if cast(bool, official["successful"])
            else PilotClassificationV1.OBSERVED,
            "OFFICIAL_SUCCESS_AT_ACTOR_FINISH"
            if cast(bool, official["successful"])
            else "OFFICIAL_FAILURE_AFTER_ACTOR_FINISH",
        )
    elif last_fact.detail_present and last_fact.action_type == "error_env":
        termination = PilotTerminationReasonV1.ACTOR_ENVIRONMENT_FAILURE
        premature = _classification(
            PilotClassificationV1.NOT_APPLICABLE, "ACTOR_ENVIRONMENT_FAILURE_NOT_FINISH"
        )
    elif last_fact.detail_present and last_fact.action_type == "unknown":
        termination = PilotTerminationReasonV1.ACTOR_UNKNOWN
        premature = _classification(
            PilotClassificationV1.NOT_APPLICABLE, "ACTOR_UNKNOWN_NOT_FINISH"
        )
    elif len(decisions) == max_steps and last_executed:
        termination = PilotTerminationReasonV1.MAX_STEPS_EXHAUSTED
        premature = _classification(
            PilotClassificationV1.NOT_APPLICABLE, "NO_ACTOR_FINISH_AT_STEP_LIMIT"
        )
    elif not last_fact.detail_present:
        termination = PilotTerminationReasonV1.UNKNOWN_MISSING_AUDIT_DETAIL
        premature = _classification(PilotClassificationV1.UNKNOWN, "TERMINAL_AUDIT_DETAIL_MISSING")
    else:
        termination = PilotTerminationReasonV1.UNKNOWN_NONEXECUTED_ACTION
        premature = _classification(
            PilotClassificationV1.UNKNOWN, "NONEXECUTED_TERMINAL_ACTION_UNCLASSIFIED"
        )
    executed_count = sum(item["executed_action_sha256"] is not None for item in decisions)
    semantic_action_classification = (
        _classification(PilotClassificationV1.NOT_MEASURABLE, "NO_INDEPENDENT_ACTION_LABEL")
        if executed_count
        else _classification(PilotClassificationV1.NOT_APPLICABLE, "NO_EXECUTED_ACTION")
    )
    census = _census(cell["census"], "cell.census")
    return PilotCellAnalysisV1(
        sequence_index=cast(int, cell["sequence_index"]),
        task_id=cast(str, cell["task_id"]),
        host=PilotHostV1(cast(str, cell["host"])),
        arm=arm,
        source_cell_sha256=canonical_sha256(cast(JsonValue, cell)),
        official_success=cast(bool, official["successful"]),
        official_score_ppm=cast(int, official["score_ppm"]),
        steps=len(decisions),
        executed_actions=executed_count,
        termination_reason=termination,
        repeated_action=repeated_classification,
        unnecessary_action=semantic_action_classification,
        wrong_action=semantic_action_classification,
        wrong_edit=wrong_edit,
        premature_stop=premature,
        call_rates=rates,
        actor_provider_tokens=_token_summary(tuple(item.actor_tokens for item in facts)),
        sentinel_openai_tokens=_token_summary(tuple(item.sentinel_tokens for item in facts)),
        audit_detail_present_count=sum(item.detail_present for item in facts),
        audit_detail_missing_count=sum(not item.detail_present for item in facts),
        openai_calls=census["openai_calls"],
        cost_usd_micros=census["cost_usd_micros"],
        wall_time_ms=census["wall_time_ms"],
    )


def _merge_rate(
    metric: PilotRateMetricV1,
    cells: tuple[PilotCellAnalysisV1, ...],
) -> PilotRateSummaryV1:
    inputs = tuple(
        next(item for item in cell.call_rates if item.metric is metric) for cell in cells
    )
    population = sum(item.population_count for item in inputs)
    measured = sum(item.measured_denominator for item in inputs)
    positive = sum(item.positive_count or 0 for item in inputs)
    missing = sum(item.missing_count for item in inputs)
    not_applicable = sum(item.not_applicable_count for item in inputs)
    return PilotRateSummaryV1(
        metric=metric,
        unit=PilotRateUnitV1.ACTOR_CALL,
        population_count=population,
        measured_denominator=measured,
        positive_count=None if measured == 0 else positive,
        missing_count=missing,
        not_applicable_count=not_applicable,
        rate_ppm=None if measured == 0 else (positive * 1_000_000 + measured // 2) // measured,
        measurement_status=_measurement_status(
            measured=measured, missing=missing, not_applicable=not_applicable
        ),
        reason_codes=tuple(sorted({reason for item in inputs for reason in item.reason_codes})),
    )


def _group(
    kind: PilotGroupKindV1,
    cells: tuple[PilotCellAnalysisV1, ...],
    *,
    group_id: str,
    host: PilotHostV1 | None = None,
    arm: PilotArmV1 | None = None,
    task_id: str | None = None,
) -> PilotGroupAnalysisV1:
    successes = tuple(cell.official_success for cell in cells)
    step_values = tuple(cell.steps for cell in cells)
    return PilotGroupAnalysisV1(
        kind=kind,
        group_id=group_id,
        host=host,
        arm=arm,
        task_id=task_id,
        cell_count=len(cells),
        official_success=_rate(
            PilotRateMetricV1.OFFICIAL_SUCCESS,
            PilotRateUnitV1.CELL,
            successes,
            ("OFFICIAL_MOBILEWORLD_EVALUATOR",),
        ),
        steps=PilotStepSummaryV1(
            cell_denominator=len(step_values),
            total_steps=sum(step_values),
            minimum_steps=min(step_values),
            maximum_steps=max(step_values),
        ),
        call_rates=tuple(_merge_rate(metric, cells) for metric in _CALL_RATE_METRICS),
        termination_counts=tuple(
            PilotTerminationCountV1(
                reason=reason,
                count=sum(cell.termination_reason is reason for cell in cells),
            )
            for reason in PilotTerminationReasonV1
        ),
        actor_provider_tokens=_merge_tokens(tuple(cell.actor_provider_tokens for cell in cells)),
        sentinel_openai_tokens=_merge_tokens(tuple(cell.sentinel_openai_tokens for cell in cells)),
        openai_calls=sum(cell.openai_calls for cell in cells),
        cost_usd_micros=sum(cell.cost_usd_micros for cell in cells),
        wall_time_ms=sum(cell.wall_time_ms for cell in cells),
    )


def _matched_pair(
    baseline: PilotCellAnalysisV1,
    joint: PilotCellAnalysisV1,
) -> PilotMatchedPairV1:
    if (
        baseline.task_id != joint.task_id
        or baseline.host is not joint.host
        or baseline.arm is not PilotArmV1.BASELINE
        or joint.arm is not PilotArmV1.JOINT_SENTINEL
    ):
        raise R25AnalysisContractError("INVALID_MATCHED_PAIR", "cell pairing differs")
    outcome = (
        PilotMatchedOutcomeV1.BOTH_SUCCESS
        if baseline.official_success and joint.official_success
        else PilotMatchedOutcomeV1.BOTH_FAILURE
        if not baseline.official_success and not joint.official_success
        else PilotMatchedOutcomeV1.JOINT_IMPROVED
        if joint.official_success
        else PilotMatchedOutcomeV1.JOINT_REGRESSED
    )
    unknown_reasons = {
        PilotTerminationReasonV1.UNKNOWN_MISSING_AUDIT_DETAIL,
        PilotTerminationReasonV1.UNKNOWN_NONEXECUTED_ACTION,
    }
    return PilotMatchedPairV1(
        task_id=baseline.task_id,
        host=baseline.host,
        baseline_sequence_index=baseline.sequence_index,
        joint_sequence_index=joint.sequence_index,
        baseline_success=baseline.official_success,
        joint_success=joint.official_success,
        outcome=outcome,
        baseline_steps=baseline.steps,
        joint_steps=joint.steps,
        joint_minus_baseline_steps=joint.steps - baseline.steps,
        baseline_termination=baseline.termination_reason,
        joint_termination=joint.termination_reason,
        termination_comparable=(
            baseline.termination_reason not in unknown_reasons
            and joint.termination_reason not in unknown_reasons
        ),
        baseline_openai_calls=baseline.openai_calls,
        joint_openai_calls=joint.openai_calls,
        baseline_cost_usd_micros=baseline.cost_usd_micros,
        joint_cost_usd_micros=joint.cost_usd_micros,
        baseline_wall_time_ms=baseline.wall_time_ms,
        joint_wall_time_ms=joint.wall_time_ms,
        baseline_actor_provider_tokens=baseline.actor_provider_tokens,
        joint_actor_provider_tokens=joint.actor_provider_tokens,
        baseline_sentinel_openai_tokens=baseline.sentinel_openai_tokens,
        joint_sentinel_openai_tokens=joint.sentinel_openai_tokens,
    )


def _matched_comparison(
    pairs: tuple[PilotMatchedPairV1, ...],
    *,
    comparison_id: str,
    host: PilotHostV1 | None,
) -> PilotMatchedComparisonV1:
    comparable = tuple(item for item in pairs if item.termination_comparable)
    return PilotMatchedComparisonV1(
        comparison_id=comparison_id,
        host=host,
        pair_count=len(pairs),
        both_success_count=sum(
            item.outcome is PilotMatchedOutcomeV1.BOTH_SUCCESS for item in pairs
        ),
        both_failure_count=sum(
            item.outcome is PilotMatchedOutcomeV1.BOTH_FAILURE for item in pairs
        ),
        joint_improved_count=sum(
            item.outcome is PilotMatchedOutcomeV1.JOINT_IMPROVED for item in pairs
        ),
        joint_regressed_count=sum(
            item.outcome is PilotMatchedOutcomeV1.JOINT_REGRESSED for item in pairs
        ),
        baseline_success_count=sum(item.baseline_success for item in pairs),
        joint_success_count=sum(item.joint_success for item in pairs),
        baseline_total_steps=sum(item.baseline_steps for item in pairs),
        joint_total_steps=sum(item.joint_steps for item in pairs),
        joint_minus_baseline_total_steps=sum(item.joint_minus_baseline_steps for item in pairs),
        comparable_termination_pairs=len(comparable),
        same_termination_count=sum(
            item.baseline_termination is item.joint_termination for item in comparable
        ),
        different_termination_count=sum(
            item.baseline_termination is not item.joint_termination for item in comparable
        ),
        missing_termination_pairs=len(pairs) - len(comparable),
        baseline_openai_calls=sum(item.baseline_openai_calls for item in pairs),
        joint_openai_calls=sum(item.joint_openai_calls for item in pairs),
        baseline_cost_usd_micros=sum(item.baseline_cost_usd_micros for item in pairs),
        joint_cost_usd_micros=sum(item.joint_cost_usd_micros for item in pairs),
        baseline_wall_time_ms=sum(item.baseline_wall_time_ms for item in pairs),
        joint_wall_time_ms=sum(item.joint_wall_time_ms for item in pairs),
        baseline_actor_provider_tokens=_merge_tokens(
            tuple(item.baseline_actor_provider_tokens for item in pairs)
        ),
        joint_actor_provider_tokens=_merge_tokens(
            tuple(item.joint_actor_provider_tokens for item in pairs)
        ),
        baseline_sentinel_openai_tokens=_merge_tokens(
            tuple(item.baseline_sentinel_openai_tokens for item in pairs)
        ),
        joint_sentinel_openai_tokens=_merge_tokens(
            tuple(item.joint_sentinel_openai_tokens for item in pairs)
        ),
    )


def analyze_pilot_stage_v1(
    manifest: FrozenPilotManifestV1,
    evidence: PilotStageEvidenceV1 | JsonValue,
    *,
    audit_detail_projections: dict[str, JsonValue] | None = None,
) -> PilotAnalysisV1:
    """Analyze one complete pilot without silently changing a denominator.

    ``audit_detail_projections`` is keyed by the exact
    ``runtime_audit_detail_sha256`` committed by each actor decision.  Missing
    entries remain explicit missing observations.  Present entries must hash
    and cross-bind exactly or analysis fails closed.

    A failed/partial driver stage does not satisfy ``PilotStageEvidenceV1``.
    Passing a partial projection therefore raises ``INCOMPLETE_CELL_MATRIX``;
    callers must report the stage failure separately instead of publishing a
    success-only subset.
    """

    if type(manifest) is not FrozenPilotManifestV1:
        raise R25AnalysisContractError("UNTRUSTED_TYPE", "manifest must use its exact type")
    source = _stage_projection(evidence)
    cells, _ = _validate_stage(manifest, source)
    if audit_detail_projections is None:
        details: dict[str, JsonValue] = {}
    elif type(audit_detail_projections) is dict and all(
        type(key) is str and _SHA256.fullmatch(key) is not None for key in audit_detail_projections
    ):
        details = dict(audit_detail_projections)
    else:
        raise R25AnalysisContractError(
            "UNTRUSTED_TYPE", "audit details must use an exact SHA-keyed dictionary"
        )
    analyses = tuple(
        _cell_analysis(
            cell,
            audit_details=details,
            max_steps=manifest.max_steps_per_cell,
        )
        for cell in cells
    )
    referenced_details = {
        cast(str, decision["runtime_audit_detail_sha256"])
        for cell in cells
        for decision in (
            _object(item, "decision") for item in _array(cell["decisions"], "cell decisions")
        )
    }
    if set(details) - referenced_details:
        raise R25AnalysisContractError(
            "UNREFERENCED_AUDIT_DETAIL", "audit detail input contains an unreferenced record"
        )
    cell_index = {(cell.task_id, cell.host, cell.arm): cell for cell in analyses}
    matched_pairs = tuple(
        _matched_pair(
            cell_index[(task.task_id, host, PilotArmV1.BASELINE)],
            cell_index[(task.task_id, host, PilotArmV1.JOINT_SENTINEL)],
        )
        for task in manifest.tasks
        for host in manifest.hosts
    )
    matched_host_comparisons = tuple(
        _matched_comparison(
            tuple(item for item in matched_pairs if item.host is host),
            comparison_id=f"MATCHED:{host.value}",
            host=host,
        )
        for host in manifest.hosts
    )
    host_arm_groups = tuple(
        _group(
            PilotGroupKindV1.HOST_ARM,
            tuple(cell for cell in analyses if cell.host is host and cell.arm is arm),
            group_id=f"{host.value}:{arm.value}",
            host=host,
            arm=arm,
        )
        for host in manifest.hosts
        for arm in manifest.arms
    )
    task_groups = tuple(
        _group(
            PilotGroupKindV1.TASK,
            tuple(cell for cell in analyses if cell.task_id == task.task_id),
            group_id=f"TASK:{task.task_id}",
            task_id=task.task_id,
        )
        for task in manifest.tasks
    )
    return PilotAnalysisV1(
        source_stage_evidence_sha256=canonical_sha256(cast(JsonValue, source)),
        source_manifest_sha256=frozen_pilot_manifest_sha256(manifest),
        manifest_sha256=cast(str, source["manifest_sha256"]),
        run_id=cast(str, source["run_id"]),
        cells=analyses,
        host_arm_groups=host_arm_groups,
        task_groups=task_groups,
        matched_pairs=matched_pairs,
        matched_host_comparisons=matched_host_comparisons,
        matched_overall=_matched_comparison(
            matched_pairs, comparison_id="MATCHED:OVERALL", host=None
        ),
        overall=_group(PilotGroupKindV1.OVERALL, analyses, group_id="OVERALL"),
        limitations=tuple(
            sorted(
                (
                    "CLEAN_HISTORY_LABELS_ABSENT_FALSE_RATES_NOT_MEASURABLE",
                    "EXACT_ACTION_HASH_REPEATS_ARE_A_LOWER_BOUND",
                    "NO_INDEPENDENT_ACTION_NECESSITY_OR_CORRECTNESS_LABELS",
                    "NO_INDEPENDENT_EDIT_CORRECTNESS_LABELS",
                    "PILOT_DOES_NOT_ISOLATE_HISTORY_TRANSFORMATION_CAUSAL_EFFECT",
                    "TECHNICAL_FAILURE_IS_NOT_SEMANTIC_WRONGNESS",
                    "UNSUCCESSFUL_FINISH_IS_ONLY_MECHANICAL_PREMATURE_STOP_SIGNAL",
                )
            )
        ),
    )


def _classification_projection(value: PilotClassificationResultV1) -> dict[str, JsonValue]:
    return {"classification": value.classification.value, "reason_code": value.reason_code}


def _rate_projection(value: PilotRateSummaryV1) -> dict[str, JsonValue]:
    return {
        "measurement_status": value.measurement_status.value,
        "measured_denominator": value.measured_denominator,
        "metric": value.metric.value,
        "missing_count": value.missing_count,
        "not_applicable_count": value.not_applicable_count,
        "population_count": value.population_count,
        "positive_count": value.positive_count,
        "rate_ppm": value.rate_ppm,
        "reason_codes": list(value.reason_codes),
        "unit": value.unit.value,
    }


def _token_projection(value: PilotTokenSummaryV1) -> dict[str, JsonValue]:
    return {
        "cached_input_tokens": value.cached_input_tokens,
        "input_tokens": value.input_tokens,
        "measured_call_denominator": value.measured_call_denominator,
        "measurement_status": value.measurement_status.value,
        "missing_call_count": value.missing_call_count,
        "not_applicable_call_count": value.not_applicable_call_count,
        "output_tokens": value.output_tokens,
        "population_calls": value.population_calls,
        "total_tokens": value.total_tokens,
    }


def _group_projection(value: PilotGroupAnalysisV1) -> dict[str, JsonValue]:
    return {
        "actor_provider_tokens": _token_projection(value.actor_provider_tokens),
        "arm": None if value.arm is None else value.arm.value,
        "call_rates": [_rate_projection(item) for item in value.call_rates],
        "cell_count": value.cell_count,
        "cost_usd_micros": value.cost_usd_micros,
        "group_id": value.group_id,
        "host": None if value.host is None else value.host.value,
        "kind": value.kind.value,
        "official_success": _rate_projection(value.official_success),
        "openai_calls": value.openai_calls,
        "sentinel_openai_tokens": _token_projection(value.sentinel_openai_tokens),
        "steps": {
            "cell_denominator": value.steps.cell_denominator,
            "maximum_steps": value.steps.maximum_steps,
            "minimum_steps": value.steps.minimum_steps,
            "total_steps": value.steps.total_steps,
        },
        "task_id": value.task_id,
        "termination_counts": [
            {"count": item.count, "reason": item.reason.value} for item in value.termination_counts
        ],
        "wall_time_ms": value.wall_time_ms,
    }


def _matched_pair_projection(value: PilotMatchedPairV1) -> dict[str, JsonValue]:
    return {
        "baseline_actor_provider_tokens": _token_projection(value.baseline_actor_provider_tokens),
        "baseline_cost_usd_micros": value.baseline_cost_usd_micros,
        "baseline_openai_calls": value.baseline_openai_calls,
        "baseline_sequence_index": value.baseline_sequence_index,
        "baseline_steps": value.baseline_steps,
        "baseline_success": value.baseline_success,
        "baseline_termination": value.baseline_termination.value,
        "baseline_wall_time_ms": value.baseline_wall_time_ms,
        "baseline_sentinel_openai_tokens": _token_projection(value.baseline_sentinel_openai_tokens),
        "host": value.host.value,
        "joint_cost_usd_micros": value.joint_cost_usd_micros,
        "joint_minus_baseline_steps": value.joint_minus_baseline_steps,
        "joint_openai_calls": value.joint_openai_calls,
        "joint_sequence_index": value.joint_sequence_index,
        "joint_steps": value.joint_steps,
        "joint_success": value.joint_success,
        "joint_actor_provider_tokens": _token_projection(value.joint_actor_provider_tokens),
        "joint_sentinel_openai_tokens": _token_projection(value.joint_sentinel_openai_tokens),
        "joint_termination": value.joint_termination.value,
        "joint_wall_time_ms": value.joint_wall_time_ms,
        "outcome": value.outcome.value,
        "task_id": value.task_id,
        "termination_comparable": value.termination_comparable,
    }


def _matched_comparison_projection(
    value: PilotMatchedComparisonV1,
) -> dict[str, JsonValue]:
    return {
        "baseline_actor_provider_tokens": _token_projection(value.baseline_actor_provider_tokens),
        "baseline_cost_usd_micros": value.baseline_cost_usd_micros,
        "baseline_openai_calls": value.baseline_openai_calls,
        "baseline_success_count": value.baseline_success_count,
        "baseline_total_steps": value.baseline_total_steps,
        "baseline_wall_time_ms": value.baseline_wall_time_ms,
        "baseline_sentinel_openai_tokens": _token_projection(value.baseline_sentinel_openai_tokens),
        "both_failure_count": value.both_failure_count,
        "both_success_count": value.both_success_count,
        "comparable_termination_pairs": value.comparable_termination_pairs,
        "comparison_id": value.comparison_id,
        "different_termination_count": value.different_termination_count,
        "host": None if value.host is None else value.host.value,
        "joint_cost_usd_micros": value.joint_cost_usd_micros,
        "joint_improved_count": value.joint_improved_count,
        "joint_minus_baseline_total_steps": value.joint_minus_baseline_total_steps,
        "joint_openai_calls": value.joint_openai_calls,
        "joint_regressed_count": value.joint_regressed_count,
        "joint_success_count": value.joint_success_count,
        "joint_total_steps": value.joint_total_steps,
        "joint_wall_time_ms": value.joint_wall_time_ms,
        "joint_actor_provider_tokens": _token_projection(value.joint_actor_provider_tokens),
        "joint_sentinel_openai_tokens": _token_projection(value.joint_sentinel_openai_tokens),
        "missing_termination_pairs": value.missing_termination_pairs,
        "pair_count": value.pair_count,
        "same_termination_count": value.same_termination_count,
    }


def pilot_analysis_projection(value: PilotAnalysisV1) -> dict[str, JsonValue]:
    """Return the canonical, hashable, denominator-complete analysis."""

    if type(value) is not PilotAnalysisV1:
        raise R25AnalysisContractError("UNTRUSTED_TYPE", "analysis must use its exact type")
    # Re-run the top-level invariants before projecting a possibly tampered
    # frozen instance.
    PilotAnalysisV1(
        source_stage_evidence_sha256=value.source_stage_evidence_sha256,
        source_manifest_sha256=value.source_manifest_sha256,
        manifest_sha256=value.manifest_sha256,
        run_id=value.run_id,
        cells=tuple(value.cells),
        host_arm_groups=tuple(value.host_arm_groups),
        task_groups=tuple(value.task_groups),
        matched_pairs=tuple(value.matched_pairs),
        matched_host_comparisons=tuple(value.matched_host_comparisons),
        matched_overall=value.matched_overall,
        overall=value.overall,
        limitations=tuple(value.limitations),
        schema_version=value.schema_version,
    )
    return {
        "cells": [
            {
                "actor_provider_tokens": _token_projection(cell.actor_provider_tokens),
                "arm": cell.arm.value,
                "audit_detail_missing_count": cell.audit_detail_missing_count,
                "audit_detail_present_count": cell.audit_detail_present_count,
                "call_rates": [_rate_projection(item) for item in cell.call_rates],
                "cost_usd_micros": cell.cost_usd_micros,
                "executed_actions": cell.executed_actions,
                "host": cell.host.value,
                "official_score_ppm": cell.official_score_ppm,
                "official_success": cell.official_success,
                "openai_calls": cell.openai_calls,
                "premature_stop": _classification_projection(cell.premature_stop),
                "repeated_action": _classification_projection(cell.repeated_action),
                "sequence_index": cell.sequence_index,
                "sentinel_openai_tokens": _token_projection(cell.sentinel_openai_tokens),
                "source_cell_sha256": cell.source_cell_sha256,
                "steps": cell.steps,
                "task_id": cell.task_id,
                "termination_reason": cell.termination_reason.value,
                "unnecessary_action": _classification_projection(cell.unnecessary_action),
                "wall_time_ms": cell.wall_time_ms,
                "wrong_action": _classification_projection(cell.wrong_action),
                "wrong_edit": _classification_projection(cell.wrong_edit),
            }
            for cell in value.cells
        ],
        "host_arm_groups": [_group_projection(item) for item in value.host_arm_groups],
        "limitations": list(value.limitations),
        "manifest_sha256": value.manifest_sha256,
        "matched_host_comparisons": [
            _matched_comparison_projection(item) for item in value.matched_host_comparisons
        ],
        "matched_overall": _matched_comparison_projection(value.matched_overall),
        "matched_pairs": [_matched_pair_projection(item) for item in value.matched_pairs],
        "overall": _group_projection(value.overall),
        "run_id": value.run_id,
        "schema_version": value.schema_version,
        "source_manifest_sha256": value.source_manifest_sha256,
        "source_stage_evidence_sha256": value.source_stage_evidence_sha256,
        "task_groups": [_group_projection(item) for item in value.task_groups],
    }


def pilot_analysis_sha256(value: PilotAnalysisV1) -> str:
    return canonical_sha256(cast(JsonValue, pilot_analysis_projection(value)))


__all__ = [
    "PILOT_ANALYSIS_SCHEMA_VERSION",
    "PilotAnalysisV1",
    "PilotCellAnalysisV1",
    "PilotClassificationResultV1",
    "PilotClassificationV1",
    "PilotGroupAnalysisV1",
    "PilotGroupKindV1",
    "PilotMatchedComparisonV1",
    "PilotMatchedOutcomeV1",
    "PilotMatchedPairV1",
    "PilotMeasurementStatusV1",
    "PilotRateMetricV1",
    "PilotRateSummaryV1",
    "PilotRateUnitV1",
    "PilotStepSummaryV1",
    "PilotTokenSummaryV1",
    "PilotTerminationCountV1",
    "PilotTerminationReasonV1",
    "R25AnalysisContractError",
    "analyze_pilot_stage_v1",
    "pilot_analysis_projection",
    "pilot_analysis_sha256",
]
