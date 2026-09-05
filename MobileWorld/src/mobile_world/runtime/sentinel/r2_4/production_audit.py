"""Owner-authorized production actor audit for the R2.4/R2.5 live path.

This module is deliberately separate from :mod:`audit_detail`.  The latter is
an accepted CPU/fake evidence contract and must not be widened to attest live
provider or MobileWorld execution.  Its owner-only detail retains trusted raw,
History-IR, policy/rubric, render/diff, validator and parsed-action projections.
Actor SDK request and provider-response bytes are referenced through their
existing Collector event/blob locators.  Each rubric attempt additionally
retains its exact canonical request preimage here because that response-
independent proof is required to audit failed or cancelled dispatches.
Credentials, environment variables, parser input, and provider reasoning are
never copied into the hash-only terminal section.

The external sink is two-phase.  ``begin`` creates and fsyncs an owner-only
0600 transaction before actor-provider dispatch.  ``commit`` atomically
publishes the terminal record under an owner-only 0700 directory.  A failed
begin prevents a transformed request from reaching the provider and creates a
module-sealed recovery receipt containing the complete detached pre-provider
preimage for the outer durable journal.  A failed terminal commit is likewise
observable and cannot be replaced by a fabricated hash or success flag.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from dataclasses import InitVar, dataclass, fields
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from threading import Lock
from time import monotonic_ns
from typing import Any, Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonValue,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
)
from mobile_world.offline.causal_replay.core import validate_history_ir
from mobile_world.runtime.sentinel.contracts import (
    SentinelFallbackReason,
    SentinelMode,
    SentinelReceipt,
    SentinelResult,
    SentinelValidationStatus,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    multi_path_rubric_projection,
    path_relevance_output_projection,
    path_relevance_output_sha256,
    rubric_tracking_state_projection,
    tracker_proposal_projection,
)
from mobile_world.runtime.sentinel.r2_3.session import (
    RubricSessionResultV1,
    RubricSessionStatus,
)
from mobile_world.runtime.sentinel.r2_4.audit_detail import (
    ParserResultStatusV1,
    trusted_history_ir_projection,
)
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    RuntimeCodecOverlayDeclarationV1,
    RuntimeHistoryExtractionResultV1,
    RuntimeHistoryExtractionStatusV1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    RuntimeVerticalExecutionScope,
    RuntimeVerticalPolicyOutputV1,
    RuntimeVerticalSentinelResultV1,
    snapshot_json_value,
    vertical_output_projection,
    vertical_output_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    live_attempt_authority_sha256,
    live_attempt_receipt_projection,
    live_attempt_receipt_root_sha256,
    live_attempt_receipt_sha256,
    snapshot_live_attempt_receipt,
)
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    LiveHistoryPolicyAttemptRequestAnchorV1,
    OwnerAuthorizedLivePerCallPolicyV1,
    ResolvedLivePolicyCallBindingV1,
    live_history_policy_attempt_request_proof_projection,
    resolved_live_policy_call_binding_projection,
    resolved_live_policy_call_binding_sha256,
    snapshot_live_history_policy_attempt_request_anchor,
    validate_live_history_policy_request_proof_projection_v1,
    validate_live_rubric_cross_bindings_v1,
)
from mobile_world.runtime.sentinel.r2_4.orchestration import (
    R24CoordinatedCallRecordV1,
    r24_coordinated_call_record_projection,
    r24_coordinated_call_record_sha256,
    rubric_session_result_projection,
    rubric_session_result_sha256,
)
from mobile_world.runtime.sentinel.r2_4.renderer import (
    RuntimeVerticalRenderResultV1,
    snapshot_vertical_render_result,
    validate_vertical_render_result,
    vertical_render_result_projection,
    vertical_render_result_sha256,
    vertical_source_mapping_projection,
    vertical_text_diff_projection,
)
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    LiveRubricAttemptRequestAnchorV1,
    LiveRubricCallReceiptV1,
    LiveRubricCallTrustAnchorV1,
    LiveRubricError,
    LiveRubricExecutionScopeV1,
    R24RubricBackendExtensionDescriptorV1,
    live_rubric_attempt_constraint_binding_projection,
    live_rubric_attempt_request_proof_projection,
    live_rubric_call_receipt_projection,
    live_rubric_call_receipt_sha256,
    r24_rubric_backend_extension_descriptor_projection,
    snapshot_live_rubric_attempt_request_anchor,
    snapshot_live_rubric_call_receipt,
    snapshot_r24_rubric_backend_extension_descriptor,
    validate_live_rubric_request_proof_projection_v1,
)
from mobile_world.runtime.sentinel.r2_4.run_fatal import (
    ProductionRunFatalError,
    ProductionRunFatalLatchV1,
    build_production_run_fatal_latch_v1,
)

PRODUCTION_RUNTIME_AUDIT_PRE_PROVIDER_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-production-audit-pre-provider/v1"
)
PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-production-audit-detail/v1"
)
PRODUCTION_RUNTIME_AUDIT_RECEIPT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-production-audit-receipt/v1"
)
PRODUCTION_RUNTIME_AUDIT_FAILURE_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-production-audit-failure/v1"
)
PRODUCTION_RUNTIME_AUDIT_FAILURE_RECEIPT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-production-audit-failure-receipt/v1"
)
PRODUCTION_RUNTIME_AUDIT_COMMIT_FAILURE_RECEIPT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-production-audit-commit-failure-receipt/v1"
)
PRODUCTION_RUNTIME_AUDIT_ADMISSION_FAILURE_RECEIPT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-production-audit-admission-failure-receipt/v1"
)
PRODUCTION_ACTOR_PROVIDER_ATTEMPT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-production-actor-provider-attempt/v1"
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_FORBIDDEN_ACTION_KEYS = frozenset(
    {
        "analysis",
        "chain_of_thought",
        "reasoning",
        "reasoning_content",
        "reasoning_text",
        "thought",
    }
)
_MAX_DETAIL_BYTES = 256 * 1024 * 1024
_PRE_PROVIDER_SEAL = object()
_DETAIL_SEAL = object()
_RECEIPT_SEAL = object()
_FAILURE_RECEIPT_SEAL = object()
_COMMIT_FAILURE_RECEIPT_SEAL = object()
_ADMISSION_FAILURE_RECEIPT_SEAL = object()


class ProductionRuntimeAuditError(R24ContractError):
    """Typed production-audit failure."""


class ProductionActorProviderAttemptStatusV1(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ProductionRuntimeAuditPreProviderStatusV1(StrEnum):
    READY = "READY"
    FALLBACK_ORIGINAL = "FALLBACK_ORIGINAL"
    BYPASSED_ORIGINAL = "BYPASSED_ORIGINAL"
    OFF = "OFF"


class ProductionRuntimeAuditPreProviderOutcomeV1(StrEnum):
    READY = "READY"
    NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL = "NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL"
    GENERIC_FALLBACK_ORIGINAL = "GENERIC_FALLBACK_ORIGINAL"
    BYPASSED_ORIGINAL = "BYPASSED_ORIGINAL"
    OFF = "OFF"


class ProductionRuntimeAuditTerminalKindV1(StrEnum):
    ACTION_EXECUTION = "ACTION_EXECUTION"
    ACTOR_FAILURE = "ACTOR_FAILURE"


class ProductionRuntimeAuditPublicationStatusV1(StrEnum):
    COMMIT_OUTCOME_UNKNOWN = "COMMIT_OUTCOME_UNKNOWN"
    ADMISSION_OUTCOME_UNKNOWN = "ADMISSION_OUTCOME_UNKNOWN"


class ProductionRuntimeAuditAdmissionStageV1(StrEnum):
    """Narrow stage at which pre-provider sink admission stopped."""

    SINK_BEGIN = "SINK_BEGIN"
    ROOT_OPEN = "ROOT_OPEN"
    DESTINATION_CHECK = "DESTINATION_CHECK"
    TEMPORARY_CREATE = "TEMPORARY_CREATE"
    ADMISSION_WRITE = "ADMISSION_WRITE"
    ADMISSION_FILE_FSYNC = "ADMISSION_FILE_FSYNC"
    ADMISSION_DIRECTORY_FSYNC = "ADMISSION_DIRECTORY_FSYNC"
    TRANSACTION_BINDING = "TRANSACTION_BINDING"


class ProductionRuntimeAuditSinkAdmissionError(ProductionRuntimeAuditError):
    """Typed, content-free signal from a staged external-sink ``begin``."""

    def __init__(
        self,
        stage: ProductionRuntimeAuditAdmissionStageV1,
        sink_exception_type: str,
    ) -> None:
        if type(stage) is not ProductionRuntimeAuditAdmissionStageV1:
            raise TypeError("production audit admission stage type differs")
        _require_id(sink_exception_type, "sink_exception_type", semantic=True)
        self.admission_stage = stage
        self.sink_exception_type = sink_exception_type
        super().__init__(
            "AUDIT_SINK_ADMISSION_FAILED",
            f"production audit sink admission failed at {stage.value}",
        )


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ProductionRuntimeAuditError("INVALID_SHA256", f"{label} is not SHA-256")
    return value


def _require_id(value: object, label: str, *, semantic: bool = False) -> str:
    pattern = _SAFE_ID if semantic else _RUNTIME_ID
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ProductionRuntimeAuditError("INVALID_RUNTIME_ID", f"{label} is invalid")
    return value


def _canonical_snapshot(value: JsonValue) -> JsonValue:
    try:
        return snapshot_json_value(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProductionRuntimeAuditError(
            "NON_CANONICAL_JSON", "production audit input is not canonical JSON"
        ) from exc


def _exception_type_label(exc: Exception) -> str:
    """Return a bounded class label without persisting an exception message."""

    label = type(exc).__name__
    if _SAFE_ID.fullmatch(label) is None:
        return "Exception"
    return label


def _overlay_projection(value: RuntimeCodecOverlayDeclarationV1) -> dict[str, JsonValue]:
    if type(value) is not RuntimeCodecOverlayDeclarationV1:
        raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "codec overlay type differs")
    return {
        "schema_version": value.schema_version,
        "overlay_id": value.overlay_id,
        "host_id": value.host_id,
        "history_family": value.history_family.value,
        "base_codec_id": value.base_codec_id,
        "base_codec_contract_version": value.base_codec_contract_version,
        "base_capability_sha256": value.base_capability_sha256,
        "implementation_sha256": value.implementation_sha256,
        "discovery_mode": value.discovery_mode.value,
        "live_ready": value.live_ready,
    }


def _sentinel_receipt_projection(value: SentinelReceipt) -> dict[str, JsonValue]:
    if type(value) is not SentinelReceipt:
        raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "Sentinel receipt type differs")
    return {
        "schema_version": value.schema_version,
        "logical_call_id": value.logical_call_id,
        "host_id": value.host_id,
        "call_role": value.call_role.value,
        "configured_mode": value.configured_mode.value,
        "effective_mode": value.effective_mode.value,
        "bypass_reason": None if value.bypass_reason is None else value.bypass_reason.value,
        "global_kill_switch_active": value.global_kill_switch_active,
        "history_codec_id": value.history_codec_id,
        "history_codec_contract_version": value.history_codec_contract_version,
        "policy_id": value.policy_id,
        "policy_output_sha256": value.policy_output_sha256,
        "raw_request_sha256": value.raw_request_sha256,
        "candidate_request_sha256": value.candidate_request_sha256,
        "final_request_sha256": value.final_request_sha256,
        "exact_diff_sha256": value.exact_diff_sha256,
        "decision_kinds": [item.value for item in value.decision_kinds],
        "policy_evaluated": value.policy_evaluated,
        "would_edit": value.would_edit,
        "edit_applied": value.edit_applied,
        "fallback_reason": None if value.fallback_reason is None else value.fallback_reason.value,
        "validation_status": value.validation_status.value,
        "validation_checks": list(value.validation_checks),
        "latency_ns": value.latency_ns,
        "request_views_persisted": value.request_views_persisted,
        "exact_diffs_persisted": value.exact_diffs_persisted,
    }


def _extraction_projection(
    value: RuntimeHistoryExtractionResultV1,
) -> dict[str, JsonValue]:
    if (
        type(value) is not RuntimeHistoryExtractionResultV1
        or value.status is not RuntimeHistoryExtractionStatusV1.READY
        or type(value.history_ir) is not HistoryIR
    ):
        raise ProductionRuntimeAuditError(
            "READY_EXTRACTION_REQUIRED", "production audit requires exact READY extraction"
        )
    history_projection = trusted_history_ir_projection(value.history_ir)
    return {
        "schema_version": value.schema_version,
        "status": value.status.value,
        "raw_request_sha256": value.raw_request_sha256,
        "overlay": cast(JsonValue, _overlay_projection(value.overlay)),
        "overlay_sha256": canonical_sha256(cast(JsonValue, _overlay_projection(value.overlay))),
        "capabilities_sha256": canonical_sha256(value.capabilities.to_dict()),
        "history_ir_sha256": canonical_sha256(cast(JsonValue, history_projection)),
        "reason_code": value.reason_code,
        "validation_checks": list(value.validation_checks),
        "warnings": list(value.warnings),
    }


def _exact_diff_projection(value: RuntimeVerticalRenderResultV1) -> dict[str, JsonValue]:
    return {
        "text_diffs": [vertical_text_diff_projection(item) for item in value.text_diffs],
        "source_mappings": [
            vertical_source_mapping_projection(item) for item in value.source_mappings
        ],
    }


def _rubric_result_detail_projection(value: RubricSessionResultV1) -> dict[str, JsonValue]:
    """Return the trusted R2.3 decision artifacts, not only their hashes."""

    summary = rubric_session_result_projection(value)
    return {
        **summary,
        "rubric": (None if value.rubric is None else multi_path_rubric_projection(value.rubric)),
        "tracking_state": (
            None if value.state is None else rubric_tracking_state_projection(value.state)
        ),
        "tracker_proposal": (
            None if value.proposal is None else tracker_proposal_projection(value.proposal)
        ),
        "path_relevance": (
            None if value.relevance is None else path_relevance_output_projection(value.relevance)
        ),
    }


def _rubric_request_proof_detail_projection(
    anchors: tuple[LiveRubricAttemptRequestAnchorV1, ...],
    attempts: tuple[LiveAttemptReceiptV1, ...],
    extension: R24RubricBackendExtensionDescriptorV1 | None,
    expected_tracking_packet_sha256: str | None,
) -> list[JsonValue]:
    """Persist independently verifiable request preimages only in restricted detail."""

    trusted_attempts = tuple(snapshot_live_attempt_receipt(value) for value in attempts)
    rubric_attempts = tuple(
        value for value in trusted_attempts if value.role is LiveAttemptRoleV1.RUBRIC
    )
    if not anchors:
        if any(_rubric_attempt_requires_request_anchor(value) for value in rubric_attempts):
            raise ProductionRuntimeAuditError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "formed rubric attempt has no durable request proof",
            )
        return []
    if extension is None:
        raise ProductionRuntimeAuditError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "rubric request proofs have no backend extension",
        )
    try:
        trusted_anchors = tuple(
            snapshot_live_rubric_attempt_request_anchor(value) for value in anchors
        )
        if (
            len(trusted_anchors) > len(rubric_attempts)
            or tuple(value.attempt_order for value in trusted_anchors)
            != tuple(range(1, len(trusted_anchors) + 1))
            or any(
                _rubric_attempt_requires_request_anchor(attempt)
                for attempt in rubric_attempts[len(trusted_anchors) :]
            )
        ):
            raise ProductionRuntimeAuditError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "durable rubric request-proof census differs from attempts",
            )
        proofs = [
            cast(
                JsonValue,
                live_rubric_attempt_request_proof_projection(
                    anchor,
                    attempt_receipt=attempt,
                    backend_extension=extension,
                ),
            )
            for anchor, attempt in zip(
                trusted_anchors,
                rubric_attempts[: len(trusted_anchors)],
                strict=True,
            )
        ]
        for proof, attempt, anchor in zip(
            proofs,
            rubric_attempts[: len(proofs)],
            trusted_anchors,
            strict=True,
        ):
            validate_live_rubric_request_proof_projection_v1(
                proof,
                attempt_receipt=attempt,
                expected_attempt_order=cast(
                    int, cast(dict[str, JsonValue], proof)["attempt_order"]
                ),
                expected_attempt_authority_sha256=live_attempt_authority_sha256(
                    anchor.attempt_authority
                ),
                expected_constraint_binding_sha256=canonical_sha256(
                    cast(
                        JsonValue,
                        live_rubric_attempt_constraint_binding_projection(
                            anchor.constraint_binding
                        ),
                    )
                ),
                expected_manifest_sha256=anchor.attempt_authority.manifest_sha256,
                expected_preflight_sha256=anchor.attempt_authority.preflight_sha256,
                expected_case_execution_lease_sha256=(
                    anchor.attempt_authority.case_execution_lease_sha256
                ),
                expected_stage_sha256=anchor.attempt_authority.stage_sha256,
                expected_pricing_binding_sha256=(anchor.attempt_authority.pricing_binding_sha256),
                expected_transport_binding_sha256=(
                    anchor.attempt_authority.transport_binding_sha256
                ),
                expected_request_sha256=anchor.attempt_authority.request_sha256,
            )
        tracking_roots = tuple(
            cast(dict[str, JsonValue], proof)["tracking_packet_sha256"]
            for proof in proofs
            if cast(dict[str, JsonValue], proof)["operation"] == "TRACK"
        )
        if len(tracking_roots) > 1 or any(
            root != expected_tracking_packet_sha256 for root in tracking_roots
        ):
            raise ProductionRuntimeAuditError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "durable rubric proof differs from the coordinated tracking packet",
            )
        return proofs
    except LiveRubricError as exc:
        raise ProductionRuntimeAuditError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "rubric request proof cannot be projected",
        ) from exc


def _rubric_attempt_requires_request_anchor(value: LiveAttemptReceiptV1) -> bool:
    """Every terminal receipt proves that an attempt authority was formed."""

    snapshot_live_attempt_receipt(value)
    return True


def _history_request_proof_detail_projection(
    anchor: LiveHistoryPolicyAttemptRequestAnchorV1 | None,
    attempts: tuple[LiveAttemptReceiptV1, ...],
    expected_evidence_packet_sha256: str | None,
) -> JsonValue:
    """Persist one complete history request proof, or an exact absent census."""

    history_attempts = tuple(
        snapshot_live_attempt_receipt(value)
        for value in attempts
        if value.role is LiveAttemptRoleV1.HISTORY_POLICY
    )
    if not history_attempts:
        if anchor is not None:
            raise ProductionRuntimeAuditError(
                "HISTORY_REQUEST_PROOF_MISMATCH",
                "history request proof exists without an attempt",
            )
        return None
    if len(history_attempts) != 1 or anchor is None:
        raise ProductionRuntimeAuditError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "history attempt lacks one complete durable request proof",
        )
    try:
        trusted = snapshot_live_history_policy_attempt_request_anchor(anchor)
        if (
            expected_evidence_packet_sha256 is not None
            and trusted.coordinator_evidence_packet_sha256 != expected_evidence_packet_sha256
        ):
            raise ProductionRuntimeAuditError(
                "HISTORY_REQUEST_PROOF_MISMATCH",
                "history request proof differs from the Coordinator evidence root",
            )
        attempt = history_attempts[0]
        proof = cast(
            JsonValue,
            live_history_policy_attempt_request_proof_projection(
                trusted,
                attempt_receipt=attempt,
            ),
        )
        validate_live_history_policy_request_proof_projection_v1(
            proof,
            attempt_receipt=attempt,
            expected_attempt_authority_sha256=live_attempt_authority_sha256(
                trusted.attempt_authority
            ),
            expected_constraint_binding_sha256=canonical_sha256(
                cast(
                    JsonValue,
                    live_rubric_attempt_constraint_binding_projection(trusted.constraint_binding),
                )
            ),
            expected_manifest_sha256=trusted.attempt_authority.manifest_sha256,
            expected_preflight_sha256=trusted.attempt_authority.preflight_sha256,
            expected_case_execution_lease_sha256=(
                trusted.attempt_authority.case_execution_lease_sha256
            ),
            expected_stage_sha256=trusted.attempt_authority.stage_sha256,
            expected_pricing_binding_sha256=(trusted.attempt_authority.pricing_binding_sha256),
            expected_transport_binding_sha256=(trusted.attempt_authority.transport_binding_sha256),
            expected_request_sha256=trusted.attempt_authority.request_sha256,
        )
        return proof
    except ProductionRuntimeAuditError:
        raise
    except Exception as exc:
        raise ProductionRuntimeAuditError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "durable history request proof could not be reconstructed",
        ) from exc


def _collector_artifact_locator_projection(
    value: JsonValue,
    *,
    expected_event_types: frozenset[str],
) -> dict[str, JsonValue]:
    """Validate a locator for an already-persisted Collector event/blob."""

    snapshot = _canonical_snapshot(value)
    if type(snapshot) is not dict or set(snapshot) != {
        "run_id",
        "task_run_id",
        "event_type",
        "event_id",
        "event_sha256",
        "snapshot_blob",
    }:
        raise ProductionRuntimeAuditError(
            "COLLECTOR_LOCATOR_INVALID", "Collector locator fields differ"
        )
    event_type = snapshot["event_type"]
    if type(event_type) is not str or event_type not in expected_event_types:
        raise ProductionRuntimeAuditError(
            "COLLECTOR_LOCATOR_INVALID", "Collector event type differs"
        )
    for name in ("run_id", "task_run_id", "event_id"):
        _require_id(snapshot[name], f"collector_{name}", semantic=True)
    _require_sha256(snapshot["event_sha256"], "collector_event_sha256")
    blob = snapshot["snapshot_blob"]
    if blob is not None:
        if type(blob) is not dict:
            raise ProductionRuntimeAuditError(
                "COLLECTOR_LOCATOR_INVALID", "Collector blob locator is invalid"
            )
        digest = blob.get("sha256")
        if digest is not None:
            _require_sha256(digest, "collector_blob_sha256")
    return snapshot


def _reject_reasoning_keys(value: JsonValue) -> None:
    stack: list[JsonValue] = [value]
    while stack:
        item = stack.pop()
        if type(item) is dict:
            for key, child in item.items():
                if key.strip().casefold().replace("-", "_") in _FORBIDDEN_ACTION_KEYS:
                    raise ProductionRuntimeAuditError(
                        "REASONING_SHAPED_ACTION", "parsed action contains a reasoning field"
                    )
                stack.append(child)
        elif type(item) is list:
            stack.extend(item)


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAuditPreProviderV1:
    """Detached hash-only commitment for every trusted pre-provider stage."""

    logical_call_id: str
    host_id: str
    status: ProductionRuntimeAuditPreProviderStatusV1
    outcome: ProductionRuntimeAuditPreProviderOutcomeV1
    configured_mode: SentinelMode
    effective_mode: SentinelMode
    fallback_reason: SentinelFallbackReason | None
    fallback_check: str | None
    raw_request_sha256: str
    extraction_sha256: str | None
    history_ir_sha256: str | None
    codec_overlay_sha256: str | None
    vertical_output_sha256: str | None
    coordinated_record_sha256: str | None
    rubric_result_sha256: str | None
    path_relevance_output_sha256: str | None
    render_result_sha256: str | None
    candidate_request_sha256: str
    exact_diff_sha256: str
    validator_result_sha256: str
    final_request_sha256: str
    live_call_binding_sha256: str | None
    live_attempt_receipt_sha256s: tuple[str, ...]
    live_attempt_receipt_root_sha256: str | None
    case_execution_lease_sha256: str | None
    preflight_report_sha256: str | None
    factory_binding_sha256: str | None
    execution_authority_sha256: str | None
    source_transport_binding_sha256: str | None
    pricing_binding_sha256: str | None
    live_openai_calls: int
    live_cost_usd_micros: int
    live_cost_exact: bool
    restricted_stage_projection: JsonValue
    restricted_stage_projection_sha256: str
    evidence_snapshot_ns: int
    history_extract_ns: int
    rubric_ns: int
    policy_ns: int
    render_ns: int
    validator_ns: int
    pre_provider_total_ns: int
    schema_version: str = PRODUCTION_RUNTIME_AUDIT_PRE_PROVIDER_SCHEMA_VERSION
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _PRE_PROVIDER_SEAL:
            raise PermissionError("production pre-provider audit is module-owned")
        if self.schema_version != PRODUCTION_RUNTIME_AUDIT_PRE_PROVIDER_SCHEMA_VERSION:
            raise ProductionRuntimeAuditError(
                "UNKNOWN_SCHEMA_VERSION", "pre-provider schema differs"
            )
        _require_id(self.logical_call_id, "logical_call_id")
        _require_id(self.host_id, "host_id")
        if (
            type(self.status) is not ProductionRuntimeAuditPreProviderStatusV1
            or type(self.outcome) is not ProductionRuntimeAuditPreProviderOutcomeV1
            or (
                type(self.configured_mode) is not SentinelMode
                or type(self.effective_mode) is not SentinelMode
            )
        ):
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "pre-provider status differs")
        expected_outcomes = {
            ProductionRuntimeAuditPreProviderStatusV1.READY: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.READY}
            ),
            ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL: frozenset(
                {
                    ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL,
                    ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL,
                }
            ),
            ProductionRuntimeAuditPreProviderStatusV1.BYPASSED_ORIGINAL: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.BYPASSED_ORIGINAL}
            ),
            ProductionRuntimeAuditPreProviderStatusV1.OFF: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.OFF}
            ),
        }
        if self.outcome not in expected_outcomes[self.status]:
            raise ProductionRuntimeAuditError(
                "PRE_PROVIDER_OUTCOME_MISMATCH", "pre-provider outcome/status differ"
            )
        if (
            self.fallback_reason is not None
            and type(self.fallback_reason) is not SentinelFallbackReason
        ):
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "fallback reason type differs")
        if self.fallback_check is not None:
            _require_id(self.fallback_check, "fallback_check", semantic=True)
        always_hashes = (
            "raw_request_sha256",
            "candidate_request_sha256",
            "exact_diff_sha256",
            "validator_result_sha256",
            "final_request_sha256",
            "restricted_stage_projection_sha256",
        )
        for name in always_hashes:
            _require_sha256(getattr(self, name), name)
        optional_live_hashes = (
            "extraction_sha256",
            "history_ir_sha256",
            "codec_overlay_sha256",
            "vertical_output_sha256",
            "coordinated_record_sha256",
            "rubric_result_sha256",
            "path_relevance_output_sha256",
            "render_result_sha256",
            "live_call_binding_sha256",
            "live_attempt_receipt_root_sha256",
            "case_execution_lease_sha256",
            "preflight_report_sha256",
            "factory_binding_sha256",
            "execution_authority_sha256",
            "source_transport_binding_sha256",
            "pricing_binding_sha256",
        )
        live_values = tuple(getattr(self, name) for name in optional_live_hashes)
        if type(self.live_attempt_receipt_sha256s) is not tuple:
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "live attempts are not a tuple"
            )
        for digest in self.live_attempt_receipt_sha256s:
            _require_sha256(digest, "live_attempt_receipt_sha256")
        for name in ("live_openai_calls", "live_cost_usd_micros"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ProductionRuntimeAuditError("INVALID_ATTEMPT_CENSUS", f"{name} is invalid")
        if type(self.live_cost_exact) is not bool:
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "live_cost_exact is not an exact bool"
            )
        restricted = _canonical_snapshot(self.restricted_stage_projection)
        if canonical_sha256(restricted) != self.restricted_stage_projection_sha256:
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "restricted stage projection hash differs"
            )
        object.__setattr__(self, "restricted_stage_projection", restricted)
        if self.status is ProductionRuntimeAuditPreProviderStatusV1.OFF:
            if (
                self.configured_mode is not SentinelMode.OFF
                or self.effective_mode is not SentinelMode.OFF
                or self.fallback_reason is not None
                or self.fallback_check is not None
                or any(item is not None for item in live_values)
                or self.live_attempt_receipt_sha256s
                or self.live_openai_calls != 0
                or self.live_cost_usd_micros != 0
                or not self.live_cost_exact
                or self.raw_request_sha256 != self.candidate_request_sha256
                or self.raw_request_sha256 != self.final_request_sha256
            ):
                raise ProductionRuntimeAuditError(
                    "OFF_AUDIT_INVARIANT", "OFF pre-provider proof differs"
                )
        elif self.status is ProductionRuntimeAuditPreProviderStatusV1.READY:
            if (
                self.effective_mode not in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
                or self.configured_mode is not self.effective_mode
                or self.fallback_reason is not None
                or self.fallback_check is not None
                or any(item is None for item in live_values)
                or not self.live_attempt_receipt_sha256s
                or self.live_openai_calls != len(self.live_attempt_receipt_sha256s)
                or not self.live_cost_exact
            ):
                raise ProductionRuntimeAuditError(
                    "LIVE_AUDIT_INVARIANT", "live pre-provider proof differs"
                )
            for name in optional_live_hashes:
                _require_sha256(getattr(self, name), name)
        elif self.status is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL:
            rubric_hashes = (
                self.coordinated_record_sha256,
                self.rubric_result_sha256,
                self.path_relevance_output_sha256,
            )
            no_history_outcome = (
                self.outcome
                is ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL
            )
            if (
                self.configured_mode not in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
                or self.effective_mode is not SentinelMode.OFF
                or self.fallback_reason is None
                or self.fallback_check is None
                or self.raw_request_sha256 != self.candidate_request_sha256
                or self.raw_request_sha256 != self.final_request_sha256
                # A terminal attempt receipt can represent cancellation before
                # dispatch.  Count actual provider dispatches, not receipts.
                # The module-owned fallback builder validates the individual
                # receipt dispatch counts before sealing this projection.
                or self.live_openai_calls > len(self.live_attempt_receipt_sha256s)
                or (not self.live_attempt_receipt_sha256s)
                != (self.live_attempt_receipt_root_sha256 is None)
                or any(item is not None for item in rubric_hashes)
                != all(item is not None for item in rubric_hashes)
                or (
                    self.coordinated_record_sha256 is not None
                    and (
                        self.live_call_binding_sha256 is None
                        or not self.live_attempt_receipt_sha256s
                    )
                )
                or (
                    no_history_outcome
                    and (
                        self.fallback_reason
                        is not SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
                        or self.fallback_check != "r2_4_no_history_r21_v1_compatibility"
                        or any(item is None for item in rubric_hashes)
                        or len(self.live_attempt_receipt_sha256s) != 2
                        or self.live_openai_calls != 2
                        or not self.live_cost_exact
                    )
                )
                or (not no_history_outcome and any(item is not None for item in rubric_hashes))
            ):
                raise ProductionRuntimeAuditError(
                    "FALLBACK_AUDIT_INVARIANT", "fallback pre-provider proof differs"
                )
            for name in optional_live_hashes:
                value = getattr(self, name)
                if value is not None:
                    _require_sha256(value, name)
        else:
            if (
                self.configured_mode not in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
                or self.effective_mode is not SentinelMode.OFF
                or self.fallback_reason is not None
                or self.fallback_check is None
                or any(item is not None for item in live_values)
                or self.live_attempt_receipt_sha256s
                or self.live_openai_calls != 0
                or self.live_cost_usd_micros != 0
                or not self.live_cost_exact
                or self.raw_request_sha256 != self.candidate_request_sha256
                or self.raw_request_sha256 != self.final_request_sha256
            ):
                raise ProductionRuntimeAuditError(
                    "BYPASS_AUDIT_INVARIANT", "bypass pre-provider proof differs"
                )
        for name in (
            "evidence_snapshot_ns",
            "history_extract_ns",
            "rubric_ns",
            "policy_ns",
            "render_ns",
            "validator_ns",
            "pre_provider_total_ns",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ProductionRuntimeAuditError("INVALID_LATENCY", f"{name} is invalid")
        if self.pre_provider_total_ns < max(
            self.evidence_snapshot_ns,
            self.history_extract_ns,
            self.rubric_ns,
            self.policy_ns,
            self.render_ns,
            self.validator_ns,
        ):
            raise ProductionRuntimeAuditError("INVALID_LATENCY", "pre-provider total is too small")


def production_runtime_audit_pre_provider_projection(
    value: ProductionRuntimeAuditPreProviderV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionRuntimeAuditPreProviderV1:
        raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "pre-provider type differs")
    # Rebuild through the sealed constructor so post-construction tampering is
    # not silently trusted.
    trusted = ProductionRuntimeAuditPreProviderV1(
        **{
            name: getattr(value, name)
            for name in (
                "logical_call_id",
                "host_id",
                "status",
                "outcome",
                "configured_mode",
                "effective_mode",
                "fallback_reason",
                "fallback_check",
                "raw_request_sha256",
                "extraction_sha256",
                "history_ir_sha256",
                "codec_overlay_sha256",
                "vertical_output_sha256",
                "coordinated_record_sha256",
                "rubric_result_sha256",
                "path_relevance_output_sha256",
                "render_result_sha256",
                "candidate_request_sha256",
                "exact_diff_sha256",
                "validator_result_sha256",
                "final_request_sha256",
                "live_call_binding_sha256",
                "live_attempt_receipt_sha256s",
                "live_attempt_receipt_root_sha256",
                "case_execution_lease_sha256",
                "preflight_report_sha256",
                "factory_binding_sha256",
                "execution_authority_sha256",
                "source_transport_binding_sha256",
                "pricing_binding_sha256",
                "live_openai_calls",
                "live_cost_usd_micros",
                "live_cost_exact",
                "restricted_stage_projection",
                "restricted_stage_projection_sha256",
                "evidence_snapshot_ns",
                "history_extract_ns",
                "rubric_ns",
                "policy_ns",
                "render_ns",
                "validator_ns",
                "pre_provider_total_ns",
                "schema_version",
            )
        },
        _seal=_PRE_PROVIDER_SEAL,
    )
    return {
        "schema_version": trusted.schema_version,
        "logical_call_id": trusted.logical_call_id,
        "host_id": trusted.host_id,
        "status": trusted.status.value,
        "outcome": trusted.outcome.value,
        "configured_mode": trusted.configured_mode.value,
        "effective_mode": trusted.effective_mode.value,
        "fallback_reason": (
            None if trusted.fallback_reason is None else trusted.fallback_reason.value
        ),
        "fallback_check": trusted.fallback_check,
        "raw_request_sha256": trusted.raw_request_sha256,
        "extraction_sha256": trusted.extraction_sha256,
        "history_ir_sha256": trusted.history_ir_sha256,
        "codec_overlay_sha256": trusted.codec_overlay_sha256,
        "vertical_output_sha256": trusted.vertical_output_sha256,
        "coordinated_record_sha256": trusted.coordinated_record_sha256,
        "rubric_result_sha256": trusted.rubric_result_sha256,
        "path_relevance_output_sha256": trusted.path_relevance_output_sha256,
        "render_result_sha256": trusted.render_result_sha256,
        "candidate_request_sha256": trusted.candidate_request_sha256,
        "exact_diff_sha256": trusted.exact_diff_sha256,
        "validator_result_sha256": trusted.validator_result_sha256,
        "final_request_sha256": trusted.final_request_sha256,
        "live_call_binding_sha256": trusted.live_call_binding_sha256,
        "live_attempt_receipt_sha256s": list(trusted.live_attempt_receipt_sha256s),
        "live_attempt_receipt_root_sha256": trusted.live_attempt_receipt_root_sha256,
        "case_execution_lease_sha256": trusted.case_execution_lease_sha256,
        "preflight_report_sha256": trusted.preflight_report_sha256,
        "factory_binding_sha256": trusted.factory_binding_sha256,
        "execution_authority_sha256": trusted.execution_authority_sha256,
        "source_transport_binding_sha256": trusted.source_transport_binding_sha256,
        "pricing_binding_sha256": trusted.pricing_binding_sha256,
        "live_openai_calls": trusted.live_openai_calls,
        "live_cost_usd_micros": trusted.live_cost_usd_micros,
        "live_cost_exact": trusted.live_cost_exact,
        "restricted_stage_projection": trusted.restricted_stage_projection,
        "restricted_stage_projection_sha256": trusted.restricted_stage_projection_sha256,
        "latencies_ns": {
            "evidence_snapshot": trusted.evidence_snapshot_ns,
            "history_extract": trusted.history_extract_ns,
            "rubric": trusted.rubric_ns,
            "policy": trusted.policy_ns,
            "render": trusted.render_ns,
            "validator": trusted.validator_ns,
            "pre_provider_total": trusted.pre_provider_total_ns,
        },
        "content_persistence": {
            "raw_request": True,
            "history_ir": trusted.status is ProductionRuntimeAuditPreProviderStatusV1.READY,
            "policy_output": trusted.status is ProductionRuntimeAuditPreProviderStatusV1.READY,
            "rubric_output": trusted.coordinated_record_sha256 is not None,
            "rendered_request": trusted.status is ProductionRuntimeAuditPreProviderStatusV1.READY,
            "exact_diff": trusted.status is ProductionRuntimeAuditPreProviderStatusV1.READY,
            "validator_result": True,
            "provider_request": "COLLECTOR_EVENT_AND_BLOB_LOCATOR",
            "provider_response": "COLLECTOR_EVENT_AND_BLOB_LOCATOR",
            "credentials": False,
            "environment": False,
            "provider_reasoning": False,
        },
    }


def production_runtime_audit_pre_provider_sha256(
    value: ProductionRuntimeAuditPreProviderV1,
) -> str:
    return canonical_sha256(
        cast(JsonValue, production_runtime_audit_pre_provider_projection(value))
    )


def _snapshot_production_runtime_audit_pre_provider(
    value: ProductionRuntimeAuditPreProviderV1,
) -> ProductionRuntimeAuditPreProviderV1:
    """Rebuild one sealed pre-provider value and detach its restricted detail."""

    if type(value) is not ProductionRuntimeAuditPreProviderV1:
        raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "pre-provider type differs")
    return ProductionRuntimeAuditPreProviderV1(
        **{item.name: getattr(value, item.name) for item in fields(value)},
        _seal=_PRE_PROVIDER_SEAL,
    )


@dataclass(frozen=True, slots=True)
class ProductionActorProviderAttemptV1:
    attempt_id: str
    attempt_index: int
    sdk_arguments_sha256: str
    final_request_sha256: str
    collector_request_locator: JsonValue
    collector_terminal_locator: JsonValue
    status: ProductionActorProviderAttemptStatusV1
    provider_response_sha256: str | None
    response_id_sha256: str | None
    model_id_sha256: str | None
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ns: int
    failure_code: str | None
    schema_version: str = PRODUCTION_ACTOR_PROVIDER_ATTEMPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_id(self.attempt_id, "attempt_id")
        if self.schema_version != PRODUCTION_ACTOR_PROVIDER_ATTEMPT_SCHEMA_VERSION:
            raise ProductionRuntimeAuditError("UNKNOWN_SCHEMA_VERSION", "attempt schema differs")
        if type(self.attempt_index) is not int or self.attempt_index < 1:
            raise ProductionRuntimeAuditError("INVALID_ATTEMPT_CENSUS", "attempt index is invalid")
        _require_sha256(self.sdk_arguments_sha256, "sdk_arguments_sha256")
        _require_sha256(self.final_request_sha256, "final_request_sha256")
        request_locator = _collector_artifact_locator_projection(
            self.collector_request_locator,
            expected_event_types=frozenset({"model_request"}),
        )
        terminal_locator = _collector_artifact_locator_projection(
            self.collector_terminal_locator,
            expected_event_types=frozenset({"model_response", "model_attempt_failed"}),
        )
        if (
            request_locator["run_id"] != terminal_locator["run_id"]
            or request_locator["task_run_id"] != terminal_locator["task_run_id"]
        ):
            raise ProductionRuntimeAuditError(
                "COLLECTOR_LOCATOR_MISMATCH", "Collector attempt locators differ"
            )
        object.__setattr__(self, "collector_request_locator", request_locator)
        object.__setattr__(self, "collector_terminal_locator", terminal_locator)
        if type(self.status) is not ProductionActorProviderAttemptStatusV1:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "attempt status type differs")
        for digest_value, label in (
            (self.provider_response_sha256, "provider_response_sha256"),
            (self.response_id_sha256, "response_id_sha256"),
            (self.model_id_sha256, "model_id_sha256"),
        ):
            if digest_value is not None:
                _require_sha256(digest_value, label)
        if self.finish_reason is not None and (
            type(self.finish_reason) is not str or _SAFE_ID.fullmatch(self.finish_reason) is None
        ):
            raise ProductionRuntimeAuditError(
                "INVALID_PROVIDER_METADATA", "finish reason is unsafe"
            )
        for token_value, label in (
            (self.input_tokens, "input_tokens"),
            (self.output_tokens, "output_tokens"),
            (self.total_tokens, "total_tokens"),
        ):
            if token_value is not None and (type(token_value) is not int or token_value < 0):
                raise ProductionRuntimeAuditError(
                    "INVALID_PROVIDER_METADATA", f"{label} is invalid"
                )
        if self.total_tokens is not None and (
            self.input_tokens is None
            or self.output_tokens is None
            or self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ProductionRuntimeAuditError("INVALID_PROVIDER_METADATA", "token census differs")
        if type(self.latency_ns) is not int or self.latency_ns < 0:
            raise ProductionRuntimeAuditError("INVALID_LATENCY", "provider latency is invalid")
        if self.status is ProductionActorProviderAttemptStatusV1.SUCCEEDED:
            if (
                self.provider_response_sha256 is None
                or self.failure_code is not None
                or terminal_locator["event_type"] != "model_response"
                or terminal_locator["snapshot_blob"] is None
            ):
                raise ProductionRuntimeAuditError(
                    "INVALID_PROVIDER_ATTEMPT", "success proof is incomplete"
                )
        elif (
            self.provider_response_sha256 is not None
            or self.failure_code != "PROVIDER_EXCEPTION"
            or terminal_locator["event_type"] != "model_attempt_failed"
        ):
            raise ProductionRuntimeAuditError(
                "INVALID_PROVIDER_ATTEMPT", "failure proof is inconsistent"
            )


def production_actor_provider_attempt_projection(
    value: ProductionActorProviderAttemptV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionActorProviderAttemptV1:
        raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "provider attempt type differs")
    return {
        "schema_version": value.schema_version,
        "attempt_id": value.attempt_id,
        "attempt_index": value.attempt_index,
        "sdk_arguments_sha256": value.sdk_arguments_sha256,
        "final_request_sha256": value.final_request_sha256,
        "collector_request_locator": value.collector_request_locator,
        "collector_terminal_locator": value.collector_terminal_locator,
        "status": value.status.value,
        "provider_response_sha256": value.provider_response_sha256,
        "response_id_sha256": value.response_id_sha256,
        "model_id_sha256": value.model_id_sha256,
        "finish_reason": value.finish_reason,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "total_tokens": value.total_tokens,
        "latency_ns": value.latency_ns,
        "failure_code": value.failure_code,
        "provider_response_persisted": False,
        "reasoning_persisted": False,
    }


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAuditDetailV1:
    """Terminal hash chain for one parsed actor decision."""

    detail_id: str
    logical_call_id: str
    pre_provider: ProductionRuntimeAuditPreProviderV1
    pre_provider_sha256: str
    sentinel_receipt_sha256: str
    actor_provider_attempts: tuple[ProductionActorProviderAttemptV1, ...]
    actor_provider_attempt_root_sha256: str
    successful_provider_response_sha256: str
    normalized_actor_output_sha256: str
    parser_input_sha256: str
    parser_id: str
    parser_status: ParserResultStatusV1
    parser_attempt_count: int
    parsed_action: JsonValue
    parsed_action_sha256: str
    action_executed: bool
    executed_action_sha256: str | None
    provider_total_ns: int
    parser_ns: int
    action_execution_ns: int
    total_ns: int
    schema_version: str = PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _DETAIL_SEAL:
            raise PermissionError("production audit detail is module-owned")
        if self.schema_version != PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION:
            raise ProductionRuntimeAuditError("UNKNOWN_SCHEMA_VERSION", "detail schema differs")
        _require_id(self.detail_id, "detail_id")
        _require_id(self.logical_call_id, "logical_call_id")
        if type(self.pre_provider) is not ProductionRuntimeAuditPreProviderV1:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "pre-provider value differs")
        if self.logical_call_id != self.pre_provider.logical_call_id:
            raise ProductionRuntimeAuditError("TRACE_BINDING_MISMATCH", "logical calls differ")
        if (
            production_runtime_audit_pre_provider_sha256(self.pre_provider)
            != self.pre_provider_sha256
        ):
            raise ProductionRuntimeAuditError("TRACE_BINDING_MISMATCH", "pre-provider hash differs")
        for name in (
            "pre_provider_sha256",
            "sentinel_receipt_sha256",
            "actor_provider_attempt_root_sha256",
            "successful_provider_response_sha256",
            "normalized_actor_output_sha256",
            "parser_input_sha256",
            "parsed_action_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        if type(self.actor_provider_attempts) is not tuple or not self.actor_provider_attempts:
            raise ProductionRuntimeAuditError("INVALID_ATTEMPT_CENSUS", "actor attempts are empty")
        if tuple(item.attempt_index for item in self.actor_provider_attempts) != tuple(
            range(1, len(self.actor_provider_attempts) + 1)
        ):
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "actor attempt order differs"
            )
        succeeded = tuple(
            item
            for item in self.actor_provider_attempts
            if item.status is ProductionActorProviderAttemptStatusV1.SUCCEEDED
        )
        if (
            not succeeded
            or succeeded[-1].provider_response_sha256 != self.successful_provider_response_sha256
        ):
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "successful response differs"
            )
        expected_root = canonical_sha256(
            cast(
                JsonValue,
                {
                    "schema_version": "mobileworld.runtime.sentinel-r2.4-production-actor-attempt-root/v1",
                    "attempt_sha256s": [
                        canonical_sha256(
                            cast(JsonValue, production_actor_provider_attempt_projection(item))
                        )
                        for item in self.actor_provider_attempts
                    ],
                },
            )
        )
        if expected_root != self.actor_provider_attempt_root_sha256:
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "actor attempt root differs"
            )
        _require_id(self.parser_id, "parser_id", semantic=True)
        if type(self.parser_status) is not ParserResultStatusV1:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "parser status differs")
        if type(self.parser_attempt_count) is not int or self.parser_attempt_count < 1:
            raise ProductionRuntimeAuditError("INVALID_PARSER_METADATA", "parser count is invalid")
        action = _canonical_snapshot(self.parsed_action)
        _reject_reasoning_keys(action)
        if canonical_sha256(action) != self.parsed_action_sha256:
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "parsed action hash differs"
            )
        object.__setattr__(self, "parsed_action", action)
        if type(self.action_executed) is not bool:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "action_executed is not exact bool")
        if self.action_executed != (self.executed_action_sha256 is not None):
            raise ProductionRuntimeAuditError(
                "ACTION_BINDING_MISMATCH", "action execution proof differs"
            )
        if (
            self.executed_action_sha256 is not None
            and self.executed_action_sha256 != self.parsed_action_sha256
        ):
            raise ProductionRuntimeAuditError("ACTION_BINDING_MISMATCH", "executed action differs")
        for name in ("provider_total_ns", "parser_ns", "action_execution_ns", "total_ns"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ProductionRuntimeAuditError("INVALID_LATENCY", f"{name} is invalid")
        minimum_total = (
            self.pre_provider.pre_provider_total_ns
            + self.provider_total_ns
            + self.parser_ns
            + self.action_execution_ns
        )
        if self.total_ns < minimum_total:
            raise ProductionRuntimeAuditError(
                "INVALID_LATENCY", "terminal total latency is too small"
            )


def production_runtime_audit_detail_projection(
    value: ProductionRuntimeAuditDetailV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionRuntimeAuditDetailV1:
        raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "production detail type differs")
    # __post_init__ has already bound all child values; projections below also
    # revalidate the sealed pre-provider snapshot.
    return {
        "schema_version": value.schema_version,
        "detail_id": value.detail_id,
        "logical_call_id": value.logical_call_id,
        "pre_provider": cast(
            JsonValue, production_runtime_audit_pre_provider_projection(value.pre_provider)
        ),
        "pre_provider_sha256": value.pre_provider_sha256,
        "sentinel_receipt_sha256": value.sentinel_receipt_sha256,
        "actor_provider_attempts": [
            production_actor_provider_attempt_projection(item)
            for item in value.actor_provider_attempts
        ],
        "actor_provider_attempt_root_sha256": value.actor_provider_attempt_root_sha256,
        "terminal": {
            "successful_provider_response_sha256": value.successful_provider_response_sha256,
            "normalized_actor_output_sha256": value.normalized_actor_output_sha256,
            "provider_response_persisted": False,
            "parser_input_sha256": value.parser_input_sha256,
            "parser_input_persisted": False,
            "parser_id": value.parser_id,
            "parser_status": value.parser_status.value,
            "parser_attempt_count": value.parser_attempt_count,
            "parsed_action": value.parsed_action,
            "parsed_action_sha256": value.parsed_action_sha256,
            "action_executed": value.action_executed,
            "executed_action_sha256": value.executed_action_sha256,
            "latencies_ns": {
                "provider_total": value.provider_total_ns,
                "parser": value.parser_ns,
                "action_execution": value.action_execution_ns,
                "total": value.total_ns,
            },
            "credentials_persisted": False,
            "environment_persisted": False,
            "reasoning_persisted": False,
        },
    }


def production_runtime_audit_detail_sha256(value: ProductionRuntimeAuditDetailV1) -> str:
    return canonical_sha256(cast(JsonValue, production_runtime_audit_detail_projection(value)))


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAuditReceiptV1:
    detail_id: str
    logical_call_id: str
    raw_request_sha256: str
    final_request_sha256: str
    provider_request_sha256: str
    provider_response_sha256: str
    exact_diff_sha256: str
    pre_provider_sha256: str
    pre_provider_status: ProductionRuntimeAuditPreProviderStatusV1
    pre_provider_outcome: ProductionRuntimeAuditPreProviderOutcomeV1
    fallback_reason: SentinelFallbackReason | None
    fallback_check: str | None
    live_call_binding_sha256: str | None
    live_attempt_receipt_root_sha256: str | None
    actor_provider_attempt_root_sha256: str
    sentinel_receipt_sha256: str
    parser_input_sha256: str
    parser_result_sha256: str
    parsed_action_sha256: str
    action_executed: bool
    executed_action_sha256: str | None
    provider_attempt_count: int
    live_openai_calls: int
    live_cost_usd_micros: int
    live_cost_exact: bool
    total_ns: int
    detail_sha256: str
    schema_version: str = PRODUCTION_RUNTIME_AUDIT_RECEIPT_SCHEMA_VERSION
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _RECEIPT_SEAL:
            raise PermissionError("production audit receipt is module-owned")
        if self.schema_version != PRODUCTION_RUNTIME_AUDIT_RECEIPT_SCHEMA_VERSION:
            raise ProductionRuntimeAuditError("UNKNOWN_SCHEMA_VERSION", "receipt schema differs")
        _require_id(self.detail_id, "detail_id")
        _require_id(self.logical_call_id, "logical_call_id")
        if (
            type(self.pre_provider_status) is not ProductionRuntimeAuditPreProviderStatusV1
            or type(self.pre_provider_outcome) is not ProductionRuntimeAuditPreProviderOutcomeV1
        ):
            raise ProductionRuntimeAuditError(
                "UNTRUSTED_TYPE", "receipt pre-provider outcome/status differs"
            )
        expected_outcomes = {
            ProductionRuntimeAuditPreProviderStatusV1.READY: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.READY}
            ),
            ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL: frozenset(
                {
                    ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL,
                    ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL,
                }
            ),
            ProductionRuntimeAuditPreProviderStatusV1.BYPASSED_ORIGINAL: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.BYPASSED_ORIGINAL}
            ),
            ProductionRuntimeAuditPreProviderStatusV1.OFF: frozenset(
                {ProductionRuntimeAuditPreProviderOutcomeV1.OFF}
            ),
        }
        if self.pre_provider_outcome not in expected_outcomes[self.pre_provider_status]:
            raise ProductionRuntimeAuditError(
                "TERMINAL_OUTCOME_MISMATCH", "receipt pre-provider outcome/status differ"
            )
        if (
            self.fallback_reason is not None
            and type(self.fallback_reason) is not SentinelFallbackReason
        ):
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "receipt fallback reason differs")
        if self.fallback_check is not None:
            _require_id(self.fallback_check, "fallback_check", semantic=True)
        if self.pre_provider_status in {
            ProductionRuntimeAuditPreProviderStatusV1.OFF,
            ProductionRuntimeAuditPreProviderStatusV1.READY,
        }:
            outcome_invalid = self.fallback_reason is not None or self.fallback_check is not None
        elif (
            self.pre_provider_status is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
        ):
            outcome_invalid = self.fallback_reason is None or self.fallback_check is None
        else:
            outcome_invalid = self.fallback_reason is not None or self.fallback_check is None
        if outcome_invalid:
            raise ProductionRuntimeAuditError(
                "TERMINAL_OUTCOME_MISMATCH", "receipt fallback classification differs"
            )
        for name in (
            "raw_request_sha256",
            "final_request_sha256",
            "provider_request_sha256",
            "provider_response_sha256",
            "exact_diff_sha256",
            "pre_provider_sha256",
            "actor_provider_attempt_root_sha256",
            "sentinel_receipt_sha256",
            "parser_input_sha256",
            "parser_result_sha256",
            "parsed_action_sha256",
            "detail_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        for name in ("live_call_binding_sha256", "live_attempt_receipt_root_sha256"):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(value, name)
        if self.provider_request_sha256 != self.final_request_sha256:
            raise ProductionRuntimeAuditError(
                "PROVIDER_FINAL_REQUEST_MISMATCH", "SDK request differs"
            )
        if self.action_executed != (self.executed_action_sha256 is not None):
            raise ProductionRuntimeAuditError("ACTION_BINDING_MISMATCH", "action proof differs")
        if (
            self.executed_action_sha256 is not None
            and self.executed_action_sha256 != self.parsed_action_sha256
        ):
            raise ProductionRuntimeAuditError("ACTION_BINDING_MISMATCH", "executed action differs")
        if type(self.provider_attempt_count) is not int or self.provider_attempt_count < 1:
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "provider census is invalid"
            )
        for name in ("live_openai_calls", "live_cost_usd_micros", "total_ns"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ProductionRuntimeAuditError("INVALID_ATTEMPT_CENSUS", f"{name} is invalid")
        if type(self.live_cost_exact) is not bool:
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "live_cost_exact is not an exact bool"
            )
        # Successful live calls always have both a call binding and an attempt
        # root.  A typed fallback may have terminal attempt evidence without a
        # completed policy binding, so only the opposite direction is invalid.
        if (
            self.live_call_binding_sha256 is not None
            and self.live_attempt_receipt_root_sha256 is None
        ):
            raise ProductionRuntimeAuditError("LIVE_AUDIT_INVARIANT", "live receipt root is absent")
        if self.live_attempt_receipt_root_sha256 is None and (
            self.live_openai_calls != 0
            or self.live_cost_usd_micros != 0
            or not self.live_cost_exact
        ):
            raise ProductionRuntimeAuditError(
                "OFF_AUDIT_INVARIANT", "receipt has unbound semantic census"
            )


def production_runtime_audit_receipt_projection(
    value: ProductionRuntimeAuditReceiptV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionRuntimeAuditReceiptV1:
        raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "audit receipt type differs")
    return {
        "schema_version": value.schema_version,
        "detail_id": value.detail_id,
        "logical_call_id": value.logical_call_id,
        "raw_request_sha256": value.raw_request_sha256,
        "final_request_sha256": value.final_request_sha256,
        "provider_request_sha256": value.provider_request_sha256,
        "provider_response_sha256": value.provider_response_sha256,
        "exact_diff_sha256": value.exact_diff_sha256,
        "pre_provider_sha256": value.pre_provider_sha256,
        "pre_provider_status": value.pre_provider_status.value,
        "pre_provider_outcome": value.pre_provider_outcome.value,
        "fallback_reason": (None if value.fallback_reason is None else value.fallback_reason.value),
        "fallback_check": value.fallback_check,
        "live_call_binding_sha256": value.live_call_binding_sha256,
        "live_attempt_receipt_root_sha256": value.live_attempt_receipt_root_sha256,
        "actor_provider_attempt_root_sha256": value.actor_provider_attempt_root_sha256,
        "sentinel_receipt_sha256": value.sentinel_receipt_sha256,
        "parser_input_sha256": value.parser_input_sha256,
        "parser_result_sha256": value.parser_result_sha256,
        "parsed_action_sha256": value.parsed_action_sha256,
        "action_executed": value.action_executed,
        "executed_action_sha256": value.executed_action_sha256,
        "provider_attempt_count": value.provider_attempt_count,
        "live_openai_calls": value.live_openai_calls,
        "live_cost_usd_micros": value.live_cost_usd_micros,
        "live_cost_exact": value.live_cost_exact,
        "total_ns": value.total_ns,
        "detail_sha256": value.detail_sha256,
    }


def production_runtime_audit_receipt_sha256(value: ProductionRuntimeAuditReceiptV1) -> str:
    return canonical_sha256(cast(JsonValue, production_runtime_audit_receipt_projection(value)))


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAuditFailureReceiptV1:
    logical_call_id: str
    raw_request_sha256: str
    final_request_sha256: str
    pre_provider_sha256: str
    actor_provider_attempt_root_sha256: str
    sentinel_receipt_sha256: str
    provider_attempt_count: int
    live_openai_calls: int
    live_cost_usd_micros: int
    live_cost_exact: bool
    failure_phase: str
    failure_code: str
    total_ns: int
    detail_sha256: str
    schema_version: str = PRODUCTION_RUNTIME_AUDIT_FAILURE_RECEIPT_SCHEMA_VERSION
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _FAILURE_RECEIPT_SEAL:
            raise PermissionError("production audit failure receipt is module-owned")
        if self.schema_version != PRODUCTION_RUNTIME_AUDIT_FAILURE_RECEIPT_SCHEMA_VERSION:
            raise ProductionRuntimeAuditError("UNKNOWN_SCHEMA_VERSION", "failure receipt differs")
        _require_id(self.logical_call_id, "logical_call_id")
        for name in (
            "raw_request_sha256",
            "final_request_sha256",
            "pre_provider_sha256",
            "actor_provider_attempt_root_sha256",
            "sentinel_receipt_sha256",
            "detail_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        _require_id(self.failure_phase, "failure_phase", semantic=True)
        _require_id(self.failure_code, "failure_code", semantic=True)
        for name in (
            "provider_attempt_count",
            "live_openai_calls",
            "live_cost_usd_micros",
            "total_ns",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ProductionRuntimeAuditError("INVALID_ATTEMPT_CENSUS", f"{name} is invalid")
        if type(self.live_cost_exact) is not bool:
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "live cost exactness differs"
            )


def production_runtime_audit_failure_receipt_projection(
    value: ProductionRuntimeAuditFailureReceiptV1,
) -> dict[str, JsonValue]:
    if type(value) is not ProductionRuntimeAuditFailureReceiptV1:
        raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "failure receipt type differs")
    return {
        "actor_provider_attempt_root_sha256": value.actor_provider_attempt_root_sha256,
        "detail_sha256": value.detail_sha256,
        "failure_code": value.failure_code,
        "failure_phase": value.failure_phase,
        "final_request_sha256": value.final_request_sha256,
        "live_cost_exact": value.live_cost_exact,
        "live_cost_usd_micros": value.live_cost_usd_micros,
        "live_openai_calls": value.live_openai_calls,
        "logical_call_id": value.logical_call_id,
        "pre_provider_sha256": value.pre_provider_sha256,
        "provider_attempt_count": value.provider_attempt_count,
        "raw_request_sha256": value.raw_request_sha256,
        "schema_version": value.schema_version,
        "sentinel_receipt_sha256": value.sentinel_receipt_sha256,
        "total_ns": value.total_ns,
    }


def production_runtime_audit_failure_receipt_sha256(
    value: ProductionRuntimeAuditFailureReceiptV1,
) -> str:
    return canonical_sha256(
        cast(JsonValue, production_runtime_audit_failure_receipt_projection(value))
    )


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAuditCommitFailureReceiptV1:
    """Recoverable terminal preimage when sink publication cannot be trusted.

    The attempted terminal is deliberately not admitted to the completed or
    failed receipt maps.  Its compact preimage retains the actor-attempt
    locators, exact action, live-call census, and terminal hashes so the outer
    stage failure journal can durably publish what happened without claiming
    that the production-audit sink committed it.  The sealed pre-provider
    projection is retained too, so request proofs survive an unknown terminal
    commit outcome after the external sink removes its temporary admission.
    """

    logical_call_id: str
    terminal_kind: ProductionRuntimeAuditTerminalKindV1
    publication_status: ProductionRuntimeAuditPublicationStatusV1
    failure_phase: str
    failure_code: str
    attempted_terminal_receipt: (
        ProductionRuntimeAuditReceiptV1 | ProductionRuntimeAuditFailureReceiptV1
    )
    attempted_terminal_receipt_sha256: str
    pre_provider: ProductionRuntimeAuditPreProviderV1
    actor_provider_attempts: tuple[ProductionActorProviderAttemptV1, ...]
    parsed_action: JsonValue | None
    schema_version: str = PRODUCTION_RUNTIME_AUDIT_COMMIT_FAILURE_RECEIPT_SCHEMA_VERSION
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _COMMIT_FAILURE_RECEIPT_SEAL:
            raise PermissionError("production audit commit-failure receipt is module-owned")
        if self.schema_version != PRODUCTION_RUNTIME_AUDIT_COMMIT_FAILURE_RECEIPT_SCHEMA_VERSION:
            raise ProductionRuntimeAuditError(
                "UNKNOWN_SCHEMA_VERSION", "commit-failure receipt schema differs"
            )
        _require_id(self.logical_call_id, "logical_call_id")
        if type(self.terminal_kind) is not ProductionRuntimeAuditTerminalKindV1 or (
            type(self.publication_status) is not ProductionRuntimeAuditPublicationStatusV1
        ):
            raise ProductionRuntimeAuditError(
                "UNTRUSTED_TYPE", "commit-failure terminal status differs"
            )
        if (
            self.failure_phase != "AUDIT_TERMINAL_COMMIT"
            or self.failure_code != "AUDIT_TERMINAL_COMMIT_FAILED"
        ):
            raise ProductionRuntimeAuditError(
                "INVALID_COMMIT_FAILURE", "commit-failure classification differs"
            )
        _require_sha256(
            self.attempted_terminal_receipt_sha256,
            "attempted_terminal_receipt_sha256",
        )
        trusted_pre_provider = _snapshot_production_runtime_audit_pre_provider(self.pre_provider)
        terminal = self.attempted_terminal_receipt
        if self.terminal_kind is ProductionRuntimeAuditTerminalKindV1.ACTION_EXECUTION:
            if type(terminal) is not ProductionRuntimeAuditReceiptV1:
                raise ProductionRuntimeAuditError(
                    "UNTRUSTED_TYPE", "action terminal receipt type differs"
                )
            terminal_hash = production_runtime_audit_receipt_sha256(terminal)
            if self.parsed_action is None:
                raise ProductionRuntimeAuditError(
                    "ACTION_BINDING_MISMATCH", "recoverable parsed action is absent"
                )
            action = _canonical_snapshot(self.parsed_action)
            _reject_reasoning_keys(action)
            if canonical_sha256(action) != terminal.parsed_action_sha256:
                raise ProductionRuntimeAuditError(
                    "ACTION_BINDING_MISMATCH", "recoverable parsed action differs"
                )
            object.__setattr__(self, "parsed_action", action)
        else:
            if type(terminal) is not ProductionRuntimeAuditFailureReceiptV1:
                raise ProductionRuntimeAuditError(
                    "UNTRUSTED_TYPE", "failed terminal receipt type differs"
                )
            terminal_hash = production_runtime_audit_failure_receipt_sha256(terminal)
            if self.parsed_action is not None:
                raise ProductionRuntimeAuditError(
                    "ACTION_BINDING_MISMATCH", "failed terminal has an action"
                )
        if (
            terminal.logical_call_id != self.logical_call_id
            or terminal_hash != self.attempted_terminal_receipt_sha256
            or trusted_pre_provider.logical_call_id != self.logical_call_id
            or production_runtime_audit_pre_provider_sha256(trusted_pre_provider)
            != terminal.pre_provider_sha256
        ):
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "recoverable terminal receipt differs"
            )
        object.__setattr__(self, "pre_provider", trusted_pre_provider)
        if type(self.actor_provider_attempts) is not tuple or any(
            type(item) is not ProductionActorProviderAttemptV1
            for item in self.actor_provider_attempts
        ):
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "recoverable actor attempts differ")
        if tuple(item.attempt_index for item in self.actor_provider_attempts) != tuple(
            range(1, len(self.actor_provider_attempts) + 1)
        ):
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "recoverable actor attempt order differs"
            )
        attempt_root = canonical_sha256(
            cast(
                JsonValue,
                {
                    "schema_version": "mobileworld.runtime.sentinel-r2.4-production-actor-attempt-root/v1",
                    "attempt_sha256s": [
                        canonical_sha256(
                            cast(JsonValue, production_actor_provider_attempt_projection(item))
                        )
                        for item in self.actor_provider_attempts
                    ],
                },
            )
        )
        if (
            attempt_root != terminal.actor_provider_attempt_root_sha256
            or len(self.actor_provider_attempts) != terminal.provider_attempt_count
        ):
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "recoverable actor attempt census differs"
            )


def _snapshot_production_runtime_audit_commit_failure_receipt(
    value: ProductionRuntimeAuditCommitFailureReceiptV1,
) -> ProductionRuntimeAuditCommitFailureReceiptV1:
    """Rebuild and detach one terminal-publication recovery receipt."""

    if type(value) is not ProductionRuntimeAuditCommitFailureReceiptV1:
        raise ProductionRuntimeAuditError(
            "UNTRUSTED_TYPE", "audit commit-failure receipt type differs"
        )
    terminal = value.attempted_terminal_receipt
    if type(terminal) is ProductionRuntimeAuditReceiptV1:
        trusted_terminal: ProductionRuntimeAuditReceiptV1 | ProductionRuntimeAuditFailureReceiptV1
        trusted_terminal = ProductionRuntimeAuditReceiptV1(
            **{item.name: getattr(terminal, item.name) for item in fields(terminal)},
            _seal=_RECEIPT_SEAL,
        )
    elif type(terminal) is ProductionRuntimeAuditFailureReceiptV1:
        trusted_terminal = ProductionRuntimeAuditFailureReceiptV1(
            **{item.name: getattr(terminal, item.name) for item in fields(terminal)},
            _seal=_FAILURE_RECEIPT_SEAL,
        )
    else:
        raise ProductionRuntimeAuditError(
            "UNTRUSTED_TYPE", "audit commit-failure terminal receipt type differs"
        )
    trusted_attempts = tuple(
        ProductionActorProviderAttemptV1(
            **{item.name: getattr(attempt, item.name) for item in fields(attempt)}
        )
        for attempt in value.actor_provider_attempts
    )
    return ProductionRuntimeAuditCommitFailureReceiptV1(
        logical_call_id=value.logical_call_id,
        terminal_kind=value.terminal_kind,
        publication_status=value.publication_status,
        failure_phase=value.failure_phase,
        failure_code=value.failure_code,
        attempted_terminal_receipt=trusted_terminal,
        attempted_terminal_receipt_sha256=value.attempted_terminal_receipt_sha256,
        pre_provider=value.pre_provider,
        actor_provider_attempts=trusted_attempts,
        parsed_action=(
            None if value.parsed_action is None else _canonical_snapshot(value.parsed_action)
        ),
        schema_version=value.schema_version,
        _seal=_COMMIT_FAILURE_RECEIPT_SEAL,
    )


def production_runtime_audit_commit_failure_receipt_projection(
    value: ProductionRuntimeAuditCommitFailureReceiptV1,
) -> dict[str, JsonValue]:
    trusted = _snapshot_production_runtime_audit_commit_failure_receipt(value)
    terminal = trusted.attempted_terminal_receipt
    if trusted.terminal_kind is ProductionRuntimeAuditTerminalKindV1.ACTION_EXECUTION:
        if type(terminal) is not ProductionRuntimeAuditReceiptV1:
            raise ProductionRuntimeAuditError(
                "UNTRUSTED_TYPE", "action terminal receipt type differs"
            )
        terminal_projection = production_runtime_audit_receipt_projection(terminal)
    else:
        if type(terminal) is not ProductionRuntimeAuditFailureReceiptV1:
            raise ProductionRuntimeAuditError(
                "UNTRUSTED_TYPE", "failed terminal receipt type differs"
            )
        terminal_projection = production_runtime_audit_failure_receipt_projection(terminal)
    return {
        "actor_provider_attempts": [
            production_actor_provider_attempt_projection(item)
            for item in trusted.actor_provider_attempts
        ],
        "attempted_terminal_receipt": cast(JsonValue, terminal_projection),
        "attempted_terminal_receipt_sha256": trusted.attempted_terminal_receipt_sha256,
        "failure_code": trusted.failure_code,
        "failure_phase": trusted.failure_phase,
        "logical_call_id": trusted.logical_call_id,
        "parsed_action": (
            None if trusted.parsed_action is None else _canonical_snapshot(trusted.parsed_action)
        ),
        "pre_provider": cast(
            JsonValue,
            production_runtime_audit_pre_provider_projection(trusted.pre_provider),
        ),
        "publication_status": trusted.publication_status.value,
        "recovery_required": True,
        "schema_version": trusted.schema_version,
        "terminal_kind": trusted.terminal_kind.value,
    }


def production_runtime_audit_commit_failure_receipt_sha256(
    value: ProductionRuntimeAuditCommitFailureReceiptV1,
) -> str:
    return canonical_sha256(
        cast(JsonValue, production_runtime_audit_commit_failure_receipt_projection(value))
    )


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAuditAdmissionFailureReceiptV1:
    """Owner-only recovery preimage when pre-provider admission has no result.

    A sink may have written and fsynced some bytes before ``begin`` raises, so
    the publication outcome is conservatively unknown.  This receipt is not a
    successful admission and cannot open actor transport.  It retains the
    complete detached pre-provider projection so an outer recovery journal can
    durably preserve every already-built rubric request proof.
    """

    logical_call_id: str
    publication_status: ProductionRuntimeAuditPublicationStatusV1
    failure_phase: str
    failure_code: str
    admission_stage: ProductionRuntimeAuditAdmissionStageV1
    sink_exception_type: str
    pre_provider: ProductionRuntimeAuditPreProviderV1
    pre_provider_sha256: str
    sentinel_receipt_sha256: str | None
    schema_version: str = PRODUCTION_RUNTIME_AUDIT_ADMISSION_FAILURE_RECEIPT_SCHEMA_VERSION
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _ADMISSION_FAILURE_RECEIPT_SEAL:
            raise PermissionError("production audit admission-failure receipt is module-owned")
        if self.schema_version != PRODUCTION_RUNTIME_AUDIT_ADMISSION_FAILURE_RECEIPT_SCHEMA_VERSION:
            raise ProductionRuntimeAuditError(
                "UNKNOWN_SCHEMA_VERSION", "admission-failure receipt schema differs"
            )
        _require_id(self.logical_call_id, "logical_call_id")
        if (
            self.publication_status
            is not ProductionRuntimeAuditPublicationStatusV1.ADMISSION_OUTCOME_UNKNOWN
            or type(self.admission_stage) is not ProductionRuntimeAuditAdmissionStageV1
            or self.failure_phase != "AUDIT_PRE_PROVIDER_ADMISSION"
            or self.failure_code != "AUDIT_PRE_PROVIDER_ADMISSION_FAILED"
        ):
            raise ProductionRuntimeAuditError(
                "INVALID_ADMISSION_FAILURE", "admission-failure classification differs"
            )
        _require_id(self.sink_exception_type, "sink_exception_type", semantic=True)
        _require_sha256(self.pre_provider_sha256, "pre_provider_sha256")
        if self.sentinel_receipt_sha256 is not None:
            _require_sha256(self.sentinel_receipt_sha256, "sentinel_receipt_sha256")
        trusted_pre_provider = _snapshot_production_runtime_audit_pre_provider(self.pre_provider)
        if (
            trusted_pre_provider.logical_call_id != self.logical_call_id
            or production_runtime_audit_pre_provider_sha256(trusted_pre_provider)
            != self.pre_provider_sha256
        ):
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "admission-failure pre-provider proof differs"
            )
        object.__setattr__(self, "pre_provider", trusted_pre_provider)


def _snapshot_production_runtime_audit_admission_failure_receipt(
    value: ProductionRuntimeAuditAdmissionFailureReceiptV1,
) -> ProductionRuntimeAuditAdmissionFailureReceiptV1:
    if type(value) is not ProductionRuntimeAuditAdmissionFailureReceiptV1:
        raise ProductionRuntimeAuditError(
            "UNTRUSTED_TYPE", "audit admission-failure receipt type differs"
        )
    return ProductionRuntimeAuditAdmissionFailureReceiptV1(
        logical_call_id=value.logical_call_id,
        publication_status=value.publication_status,
        failure_phase=value.failure_phase,
        failure_code=value.failure_code,
        admission_stage=value.admission_stage,
        sink_exception_type=value.sink_exception_type,
        pre_provider=value.pre_provider,
        pre_provider_sha256=value.pre_provider_sha256,
        sentinel_receipt_sha256=value.sentinel_receipt_sha256,
        schema_version=value.schema_version,
        _seal=_ADMISSION_FAILURE_RECEIPT_SEAL,
    )


def production_runtime_audit_admission_failure_receipt_projection(
    value: ProductionRuntimeAuditAdmissionFailureReceiptV1,
) -> dict[str, JsonValue]:
    trusted = _snapshot_production_runtime_audit_admission_failure_receipt(value)
    return {
        "admission_stage": trusted.admission_stage.value,
        "failure_code": trusted.failure_code,
        "failure_phase": trusted.failure_phase,
        "logical_call_id": trusted.logical_call_id,
        "pre_provider": cast(
            JsonValue,
            production_runtime_audit_pre_provider_projection(trusted.pre_provider),
        ),
        "pre_provider_sha256": trusted.pre_provider_sha256,
        "publication_status": trusted.publication_status.value,
        "recovery_required": True,
        "schema_version": trusted.schema_version,
        "sentinel_receipt_sha256": trusted.sentinel_receipt_sha256,
        "sink_exception_type": trusted.sink_exception_type,
    }


def production_runtime_audit_admission_failure_receipt_sha256(
    value: ProductionRuntimeAuditAdmissionFailureReceiptV1,
) -> str:
    return canonical_sha256(
        cast(JsonValue, production_runtime_audit_admission_failure_receipt_projection(value))
    )


@runtime_checkable
class ProductionRuntimeAuditTransactionV1(Protocol):
    @property
    def logical_call_id(self) -> str: ...

    @property
    def pre_provider_sha256(self) -> str: ...

    def commit(self, detail: ProductionRuntimeAuditDetailV1) -> None: ...

    def commit_failure(self, detail: JsonValue) -> None: ...

    def abort(self) -> None: ...


@runtime_checkable
class ProductionRuntimeAuditSinkV1(Protocol):
    def begin(
        self, pre_provider: ProductionRuntimeAuditPreProviderV1
    ) -> ProductionRuntimeAuditTransactionV1: ...


class _MemoryProductionRuntimeAuditTransactionV1:
    def __init__(
        self,
        sink: MemoryProductionRuntimeAuditSinkV1,
        pre_provider: ProductionRuntimeAuditPreProviderV1,
    ) -> None:
        self._sink = sink
        self._logical_call_id = pre_provider.logical_call_id
        self._pre_provider_sha256 = production_runtime_audit_pre_provider_sha256(pre_provider)
        self._done = False

    @property
    def logical_call_id(self) -> str:
        return self._logical_call_id

    @property
    def pre_provider_sha256(self) -> str:
        return self._pre_provider_sha256

    def commit(self, detail: ProductionRuntimeAuditDetailV1) -> None:
        if self._done:
            raise ProductionRuntimeAuditError("TRANSACTION_CLOSED", "audit transaction is closed")
        self._done = True
        self._sink._commit(self._logical_call_id, self._pre_provider_sha256, detail)

    def commit_failure(self, detail: JsonValue) -> None:
        if self._done:
            raise ProductionRuntimeAuditError("TRANSACTION_CLOSED", "audit transaction is closed")
        self._done = True
        self._sink._commit_failure(self._logical_call_id, self._pre_provider_sha256, detail)

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        self._sink._abort(self._logical_call_id)


class MemoryProductionRuntimeAuditSinkV1:
    """In-memory exact sink for CPU-only contract tests."""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._details: dict[str, ProductionRuntimeAuditDetailV1] = {}
        self._failure_details: dict[str, JsonValue] = {}
        self._order: list[str] = []
        self._lock = Lock()

    def begin(
        self, pre_provider: ProductionRuntimeAuditPreProviderV1
    ) -> ProductionRuntimeAuditTransactionV1:
        production_runtime_audit_pre_provider_projection(pre_provider)
        with self._lock:
            if (
                pre_provider.logical_call_id in self._active
                or pre_provider.logical_call_id in self._details
                or pre_provider.logical_call_id in self._failure_details
            ):
                raise FileExistsError("production audit logical call already exists")
            self._active.add(pre_provider.logical_call_id)
        return _MemoryProductionRuntimeAuditTransactionV1(self, pre_provider)

    def _commit(
        self,
        logical_call_id: str,
        pre_provider_sha256: str,
        detail: ProductionRuntimeAuditDetailV1,
    ) -> None:
        if (
            detail.logical_call_id != logical_call_id
            or detail.pre_provider_sha256 != pre_provider_sha256
        ):
            raise ProductionRuntimeAuditError(
                "TRANSACTION_BINDING_MISMATCH", "detail differs from begin"
            )
        production_runtime_audit_detail_projection(detail)
        with self._lock:
            if logical_call_id not in self._active or logical_call_id in self._details:
                raise ProductionRuntimeAuditError(
                    "TRANSACTION_CLOSED", "audit transaction is absent"
                )
            self._active.remove(logical_call_id)
            self._details[logical_call_id] = detail
            self._order.append(logical_call_id)

    def _commit_failure(
        self,
        logical_call_id: str,
        pre_provider_sha256: str,
        detail: JsonValue,
    ) -> None:
        projection = _canonical_snapshot(detail)
        if (
            type(projection) is not dict
            or projection.get("logical_call_id") != logical_call_id
            or projection.get("pre_provider_sha256") != pre_provider_sha256
        ):
            raise ProductionRuntimeAuditError(
                "TRANSACTION_BINDING_MISMATCH", "failure detail differs from begin"
            )
        with self._lock:
            if logical_call_id not in self._active or logical_call_id in self._failure_details:
                raise ProductionRuntimeAuditError(
                    "TRANSACTION_CLOSED", "audit transaction is absent"
                )
            self._active.remove(logical_call_id)
            self._failure_details[logical_call_id] = projection
            self._order.append(logical_call_id)

    def _abort(self, logical_call_id: str) -> None:
        with self._lock:
            self._active.discard(logical_call_id)

    @property
    def details(self) -> tuple[ProductionRuntimeAuditDetailV1, ...]:
        with self._lock:
            return tuple(self._details[item] for item in self._order if item in self._details)

    @property
    def failure_details(self) -> tuple[JsonValue, ...]:
        with self._lock:
            return tuple(
                self._failure_details[item] for item in self._order if item in self._failure_details
            )


def _discover_repository_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate.resolve()
    raise RuntimeError("cannot discover repository root")


class _ExternalProductionRuntimeAuditTransactionV1:
    def __init__(
        self,
        *,
        sink: ExternalProductionRuntimeAuditSinkV1,
        logical_call_id: str,
        pre_provider_sha256: str,
        directory_fd: int,
        file_fd: int,
        temporary: str,
        destination: str,
    ) -> None:
        self._sink = sink
        self._logical_call_id = logical_call_id
        self._pre_provider_sha256 = pre_provider_sha256
        self._directory_fd = directory_fd
        self._file_fd = file_fd
        self._temporary = temporary
        self._destination = destination
        self._done = False
        self._lock = Lock()

    @property
    def logical_call_id(self) -> str:
        return self._logical_call_id

    @property
    def pre_provider_sha256(self) -> str:
        return self._pre_provider_sha256

    def _close(self) -> None:
        for descriptor_name in ("_file_fd", "_directory_fd"):
            descriptor = getattr(self, descriptor_name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                finally:
                    setattr(self, descriptor_name, -1)

    def _commit_payload(self, payload: bytes) -> None:
        with self._lock:
            if self._done:
                raise ProductionRuntimeAuditError(
                    "TRANSACTION_CLOSED", "audit transaction is closed"
                )
            self._done = True
        published = False
        try:
            if len(payload) > _MAX_DETAIL_BYTES:
                raise ProductionRuntimeAuditError("AUDIT_DETAIL_TOO_LARGE", "detail exceeds budget")
            self._sink._validate_open_root(self._directory_fd)
            os.ftruncate(self._file_fd, 0)
            os.lseek(self._file_fd, 0, os.SEEK_SET)
            self._sink._write_all(self._file_fd, payload)
            os.fsync(self._file_fd)
            info = os.fstat(self._file_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_gid != os.getegid()
                or info.st_nlink != 1
                or info.st_size != len(payload)
            ):
                raise OSError("production audit transaction metadata changed")
            self._sink._validate_open_root(self._directory_fd)
            os.link(
                self._temporary,
                self._destination,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            published = True
            os.unlink(self._temporary, dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
            self._sink._finish(self._logical_call_id, committed=True)
        except Exception:
            if not published:
                try:
                    os.unlink(self._temporary, dir_fd=self._directory_fd)
                except OSError:
                    pass
            self._sink._finish(self._logical_call_id, committed=published)
            raise
        finally:
            self._close()

    def commit(self, detail: ProductionRuntimeAuditDetailV1) -> None:
        if (
            detail.logical_call_id != self._logical_call_id
            or detail.pre_provider_sha256 != self._pre_provider_sha256
        ):
            raise ProductionRuntimeAuditError(
                "TRANSACTION_BINDING_MISMATCH", "detail differs from begin"
            )
        self._commit_payload(
            canonical_json_bytes(
                cast(JsonValue, production_runtime_audit_detail_projection(detail))
            )
        )

    def commit_failure(self, detail: JsonValue) -> None:
        projection = _canonical_snapshot(detail)
        if (
            type(projection) is not dict
            or projection.get("logical_call_id") != self._logical_call_id
            or projection.get("pre_provider_sha256") != self._pre_provider_sha256
        ):
            raise ProductionRuntimeAuditError(
                "TRANSACTION_BINDING_MISMATCH", "failure detail differs from begin"
            )
        self._commit_payload(canonical_json_bytes(projection))

    def abort(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        try:
            os.unlink(self._temporary, dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
        except FileNotFoundError:
            pass
        finally:
            self._sink._finish(self._logical_call_id, committed=False)
            self._close()


class ExternalProductionRuntimeAuditSinkV1:
    """Atomic 0700/0600 production detail sink outside every repository root."""

    def __init__(self, root: Path, *, repository_root: Path | None = None) -> None:
        if not root.is_absolute():
            raise ValueError("production audit root must be an absolute Path")
        repositories = {_discover_repository_root()}
        if repository_root is not None:
            repositories.add(repository_root.resolve(strict=True))
        parent = root.parent.resolve(strict=True)
        resolved = parent / root.name
        if any(resolved == item or resolved.is_relative_to(item) for item in repositories):
            raise ValueError("production audit root must be outside the repository")
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid()
        ):
            raise PermissionError("production audit root must be owner-only 0700")
        self._root = resolved
        self._identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid)
        self._active: set[str] = set()
        self._committed: set[str] = set()
        self._lock = Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _open_root(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._root, flags)
        try:
            self._validate_open_root(descriptor)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        return descriptor

    def _validate_open_root(self, descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_dev, info.st_ino, info.st_uid, info.st_gid) != self._identity
        ):
            raise OSError("production audit root identity or mode changed")

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short production audit write")
            remaining = remaining[written:]

    def begin(
        self, pre_provider: ProductionRuntimeAuditPreProviderV1
    ) -> ProductionRuntimeAuditTransactionV1:
        projection = production_runtime_audit_pre_provider_projection(pre_provider)
        pre_hash = canonical_sha256(cast(JsonValue, projection))
        logical_call_id = pre_provider.logical_call_id
        destination = f"{logical_call_id}.production-runtime-audit.v1.json"
        temporary = f".{logical_call_id}.{secrets.token_hex(12)}.tmp"
        with self._lock:
            if logical_call_id in self._active or logical_call_id in self._committed:
                raise FileExistsError("production audit logical call already exists")
            self._active.add(logical_call_id)
        directory_fd = -1
        file_fd = -1
        admission_stage = ProductionRuntimeAuditAdmissionStageV1.ROOT_OPEN
        try:
            directory_fd = self._open_root()
            admission_stage = ProductionRuntimeAuditAdmissionStageV1.DESTINATION_CHECK
            try:
                os.stat(destination, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("production audit destination already exists")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            admission_stage = ProductionRuntimeAuditAdmissionStageV1.TEMPORARY_CREATE
            file_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            admission = canonical_json_bytes(
                cast(
                    JsonValue,
                    {
                        "phase": "PRE_PROVIDER_ADMITTED",
                        "logical_call_id": logical_call_id,
                        "pre_provider_sha256": pre_hash,
                    },
                )
            )
            admission_stage = ProductionRuntimeAuditAdmissionStageV1.ADMISSION_WRITE
            self._write_all(file_fd, admission)
            admission_stage = ProductionRuntimeAuditAdmissionStageV1.ADMISSION_FILE_FSYNC
            os.fsync(file_fd)
            admission_stage = ProductionRuntimeAuditAdmissionStageV1.ADMISSION_DIRECTORY_FSYNC
            os.fsync(directory_fd)
            return _ExternalProductionRuntimeAuditTransactionV1(
                sink=self,
                logical_call_id=logical_call_id,
                pre_provider_sha256=pre_hash,
                directory_fd=directory_fd,
                file_fd=file_fd,
                temporary=temporary,
                destination=destination,
            )
        except Exception as exc:
            if file_fd >= 0:
                try:
                    os.close(file_fd)
                except OSError:
                    pass
            if directory_fd >= 0:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
                try:
                    os.close(directory_fd)
                except OSError:
                    pass
            self._finish(logical_call_id, committed=False)
            raise ProductionRuntimeAuditSinkAdmissionError(
                admission_stage,
                _exception_type_label(exc),
            ) from exc

    def _finish(self, logical_call_id: str, *, committed: bool) -> None:
        with self._lock:
            self._active.discard(logical_call_id)
            if committed:
                self._committed.add(logical_call_id)


@dataclass(slots=True)
class _OpenProviderAttempt:
    attempt_id: str
    attempt_index: int
    sdk_arguments_sha256: str
    collector_request_locator: JsonValue
    started_ns: int


@dataclass(slots=True)
class _PendingProductionAudit:
    pre_provider: ProductionRuntimeAuditPreProviderV1
    transaction: ProductionRuntimeAuditTransactionV1
    sentinel_receipt_sha256: str | None
    attempts: list[ProductionActorProviderAttemptV1]
    open_attempt: _OpenProviderAttempt | None
    normalized_actor_output_sha256: str | None
    parser_input_sha256: str | None
    parser_id: str | None
    parser_status: ParserResultStatusV1 | None
    parser_attempt_count: int | None
    parsed_action: JsonValue | None
    parsed_action_sha256: str | None
    parser_ns: int | None


class ProductionRuntimeAuditV1:
    """Strict live audit coordinator used only with the per-call live policy."""

    def __init__(
        self,
        *,
        policy: OwnerAuthorizedLivePerCallPolicyV1 | None,
        sink: ProductionRuntimeAuditSinkV1,
        run_fatal_latch: ProductionRunFatalLatchV1 | None = None,
    ) -> None:
        if policy is not None and type(policy) is not OwnerAuthorizedLivePerCallPolicyV1:
            raise TypeError("production audit policy must be an exact per-call live policy or None")
        if not isinstance(sink, ProductionRuntimeAuditSinkV1):
            raise TypeError("production audit sink does not implement its protocol")
        if run_fatal_latch is not None and type(run_fatal_latch) is not ProductionRunFatalLatchV1:
            raise TypeError("production audit run-fatal latch type differs")
        self._policy = policy
        self._sink_begin = sink.begin
        self._run_fatal_latch = (
            build_production_run_fatal_latch_v1() if run_fatal_latch is None else run_fatal_latch
        )
        self._pending: dict[str, _PendingProductionAudit] = {}
        self._completed: dict[str, ProductionRuntimeAuditReceiptV1] = {}
        self._failures: dict[str, ProductionRuntimeAuditFailureReceiptV1] = {}
        self._commit_failures: dict[str, ProductionRuntimeAuditCommitFailureReceiptV1] = {}
        self._admission_failures: dict[str, ProductionRuntimeAuditAdmissionFailureReceiptV1] = {}
        self._completed_order: list[str] = []
        self._failure_order: list[str] = []
        self._commit_failure_order: list[str] = []
        self._admission_failure_order: list[str] = []
        self._lock = Lock()

    def _record_admission_failure(
        self,
        receipt: ProductionRuntimeAuditAdmissionFailureReceiptV1,
    ) -> None:
        """Retain one pre-provider recovery proof without admitting transport."""

        with self._lock:
            logical_call_id = receipt.logical_call_id
            if (
                logical_call_id in self._pending
                or logical_call_id in self._completed
                or logical_call_id in self._failures
                or logical_call_id in self._commit_failures
                or logical_call_id in self._admission_failures
            ):
                raise ProductionRuntimeAuditError(
                    "DUPLICATE_AUDIT_CALL", "admission-failure recovery receipt repeats"
                )
            self._admission_failures[logical_call_id] = receipt
            self._admission_failure_order.append(logical_call_id)

    def _record_commit_failure(
        self,
        receipt: ProductionRuntimeAuditCommitFailureReceiptV1,
    ) -> None:
        """Retain one module-built recovery terminal without admitting success."""

        with self._lock:
            logical_call_id = receipt.logical_call_id
            if (
                logical_call_id in self._completed
                or logical_call_id in self._failures
                or logical_call_id in self._commit_failures
                or logical_call_id in self._admission_failures
            ):
                raise ProductionRuntimeAuditError(
                    "DUPLICATE_AUDIT_CALL", "terminal recovery receipt repeats"
                )
            self._commit_failures[logical_call_id] = receipt
            self._commit_failure_order.append(logical_call_id)

    @property
    def live_policy(self) -> OwnerAuthorizedLivePerCallPolicyV1 | None:
        """The identity-bound live policy, or ``None`` for an exact OFF case."""

        return self._policy

    @property
    def strict_provider_audit(self) -> bool:
        return True

    @property
    def run_fatal_latch(self) -> ProductionRunFatalLatchV1:
        return self._run_fatal_latch

    def _observe_and_require_run_not_fatal(
        self,
        logical_call_id: str,
        *,
        known_attempts: tuple[LiveAttemptReceiptV1, ...] | None = None,
    ) -> None:
        attempts = known_attempts
        if attempts is None and self._policy is not None:
            try:
                attempts = self._policy.attempt_receipts_for_call(logical_call_id)
            except R24ContractError:
                attempts = ()
        try:
            if attempts is not None:
                self._run_fatal_latch.observe_attempts(
                    logical_call_id=logical_call_id,
                    attempts=attempts,
                )
            self._run_fatal_latch.require_clear()
        except ProductionRunFatalError as exc:
            raise ProductionRuntimeAuditError(exc.code, str(exc)) from exc

    def begin_pre_provider(
        self,
        *,
        logical_call_id: str,
        raw_request: JsonValue,
        extraction: RuntimeHistoryExtractionResultV1,
        policy_output: RuntimeVerticalPolicyOutputV1,
        render_result: RuntimeVerticalRenderResultV1,
        configured_mode: SentinelMode,
        effective_mode: SentinelMode,
        final_request: JsonValue,
        history_extract_ns: int,
        policy_ns: int,
        render_ns: int,
        validator_ns: int,
        pre_provider_total_ns: int,
    ) -> None:
        """Validate all live stages and acquire sink admission before transport."""

        _require_id(logical_call_id, "logical_call_id")
        raw = _canonical_snapshot(raw_request)
        final = _canonical_snapshot(final_request)
        if type(policy_output) is not RuntimeVerticalPolicyOutputV1 or (
            policy_output.execution_scope
            is not RuntimeVerticalExecutionScope.OWNER_AUTHORIZED_LIVE_ACTIVE
        ):
            raise ProductionRuntimeAuditError("LIVE_OUTPUT_REQUIRED", "policy output is not live")
        if type(render_result) is not RuntimeVerticalRenderResultV1 or (
            render_result.execution_scope
            is not RuntimeVerticalExecutionScope.OWNER_AUTHORIZED_LIVE_ACTIVE
        ):
            raise ProductionRuntimeAuditError("LIVE_RENDER_REQUIRED", "render result is not live")
        extraction_projection = _extraction_projection(extraction)
        assert extraction.history_ir is not None
        history = extraction.history_ir
        validate_history_ir(raw, history)
        render = snapshot_vertical_render_result(render_result)
        validate_vertical_render_result(raw, history, policy_output.admitted_plan, render)
        raw_hash = canonical_sha256(raw)
        final_hash = canonical_sha256(final)
        output_hash = vertical_output_sha256(policy_output)
        candidate_hash = render.candidate_request_sha256
        if (
            extraction.raw_request_sha256 != raw_hash
            or policy_output.admitted_plan.logical_call_id != logical_call_id
            or policy_output.admitted_plan.host_id != history.host_id
            or policy_output.admitted_plan.source_request_sha256 != raw_hash
            or render.source_request_sha256 != raw_hash
        ):
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "pre-provider stages differ"
            )
        if effective_mode is SentinelMode.ACTIVE:
            if final_hash != candidate_hash:
                raise ProductionRuntimeAuditError(
                    "ACTIVE_CANDIDATE_MISMATCH", "final is not candidate"
                )
        elif effective_mode is SentinelMode.SHADOW:
            if final_hash != raw_hash:
                raise ProductionRuntimeAuditError(
                    "SHADOW_ORIGINAL_MISMATCH", "SHADOW changed request"
                )
        else:
            raise ProductionRuntimeAuditError(
                "LIVE_MODE_REQUIRED", "live audit supports SHADOW/ACTIVE"
            )

        if self._policy is None:
            raise ProductionRuntimeAuditError(
                "LIVE_POLICY_MISSING", "SHADOW/ACTIVE production audit has no live policy"
            )
        binding = self._policy.call_binding(logical_call_id)
        attempts = self._policy.attempt_receipts_for_call(logical_call_id)
        rubric_call_receipts = self._policy.rubric_call_receipts_for_call(logical_call_id)
        rubric_call_trust_anchors = self._policy.rubric_call_trust_anchors_for_call(logical_call_id)
        rubric_attempt_request_anchors = self._policy.rubric_attempt_request_anchors_for_call(
            logical_call_id
        )
        history_attempt_request_anchor = (
            self._policy.history_policy_attempt_request_anchor_for_call(logical_call_id)
        )
        rubric_backend_extension = self._policy.rubric_backend_extension_descriptor()
        coordinated = self._policy.coordinated_record_for_call(logical_call_id)
        self._validate_live_bindings(
            logical_call_id=logical_call_id,
            raw_request_sha256=raw_hash,
            policy_output=policy_output,
            binding=binding,
            attempts=attempts,
            rubric_attempt_request_anchors=rubric_attempt_request_anchors,
            history_attempt_request_anchor=history_attempt_request_anchor,
            rubric_call_receipts=rubric_call_receipts,
            rubric_call_trust_anchors=rubric_call_trust_anchors,
            rubric_backend_extension=rubric_backend_extension,
            coordinated=coordinated,
        )
        assert coordinated.rubric_result.relevance is not None
        history_hash = cast(str, extraction_projection["history_ir_sha256"])
        overlay_hash = cast(str, extraction_projection["overlay_sha256"])
        exact_diff = _exact_diff_projection(render)
        exact_diff_hash = canonical_sha256(cast(JsonValue, exact_diff))
        if exact_diff_hash != render.exact_diff_sha256:
            raise ProductionRuntimeAuditError("TRACE_BINDING_MISMATCH", "exact diff hash differs")
        validator_projection: dict[str, JsonValue] = {
            "status": "PASSED",
            "logical_call_id": logical_call_id,
            "host_id": history.host_id,
            "configured_mode": configured_mode.value,
            "effective_mode": effective_mode.value,
            "raw_request_sha256": raw_hash,
            "history_ir_sha256": history_hash,
            "codec_overlay_sha256": overlay_hash,
            "vertical_output_sha256": output_hash,
            "render_result_sha256": vertical_render_result_sha256(render),
            "candidate_request_sha256": candidate_hash,
            "exact_diff_sha256": exact_diff_hash,
            "final_request_sha256": final_hash,
            "validation_checks": [
                "READY_HISTORY_EXTRACTION",
                "LIVE_POLICY_CALL_BINDING",
                "R23_COORDINATED_RUBRIC_BINDING",
                "RENDER_REVALIDATED",
                "FINAL_MODE_INVARIANT",
            ],
        }
        live_hashes = tuple(live_attempt_receipt_sha256(item) for item in attempts)
        restricted_stage_projection: JsonValue = {
            "raw_request": raw,
            "extraction": extraction_projection,
            "history_ir": cast(JsonValue, trusted_history_ir_projection(history)),
            "vertical_output": cast(JsonValue, vertical_output_projection(policy_output)),
            "coordinated_record": r24_coordinated_call_record_projection(coordinated),
            "rubric_generation_result": _rubric_result_detail_projection(
                coordinated.generation_result
            ),
            "rubric_result": _rubric_result_detail_projection(coordinated.rubric_result),
            "path_relevance_output": path_relevance_output_projection(
                coordinated.rubric_result.relevance
            ),
            "render_result": {
                **vertical_render_result_projection(render),
                "candidate_request": render.candidate_request,
                "exact_diff": exact_diff,
            },
            "final_request": final,
            "validator_result": validator_projection,
            "live_call_binding": resolved_live_policy_call_binding_projection(binding),
            "live_attempt_receipts": [
                cast(JsonValue, live_attempt_receipt_projection(item)) for item in attempts
            ],
            "r2_4_rubric_call_receipts": [
                cast(JsonValue, live_rubric_call_receipt_projection(item))
                for item in rubric_call_receipts
            ],
            "r2_4_rubric_request_proofs": _rubric_request_proof_detail_projection(
                rubric_attempt_request_anchors,
                attempts,
                rubric_backend_extension,
                coordinated.tracking_packet_sha256,
            ),
            "r2_4_history_policy_request_proof": (
                _history_request_proof_detail_projection(
                    history_attempt_request_anchor,
                    attempts,
                    coordinated.gpt56_evidence_packet_sha256,
                )
            ),
            "r2_4_rubric_backend_extension": (
                r24_rubric_backend_extension_descriptor_projection(rubric_backend_extension)
            ),
            "semantic_stage_projections_persisted": True,
            "raw_request_persisted_in_owner_only_detail": True,
            "provider_response_via_collector_locator": True,
            "provider_reasoning_persisted": False,
        }
        pre = ProductionRuntimeAuditPreProviderV1(
            logical_call_id=logical_call_id,
            host_id=history.host_id,
            status=ProductionRuntimeAuditPreProviderStatusV1.READY,
            outcome=ProductionRuntimeAuditPreProviderOutcomeV1.READY,
            configured_mode=configured_mode,
            effective_mode=effective_mode,
            fallback_reason=None,
            fallback_check=None,
            raw_request_sha256=raw_hash,
            extraction_sha256=canonical_sha256(cast(JsonValue, extraction_projection)),
            history_ir_sha256=history_hash,
            codec_overlay_sha256=overlay_hash,
            vertical_output_sha256=output_hash,
            coordinated_record_sha256=r24_coordinated_call_record_sha256(coordinated),
            rubric_result_sha256=rubric_session_result_sha256(coordinated.rubric_result),
            path_relevance_output_sha256=path_relevance_output_sha256(
                coordinated.rubric_result.relevance
            ),
            render_result_sha256=vertical_render_result_sha256(render),
            candidate_request_sha256=candidate_hash,
            exact_diff_sha256=exact_diff_hash,
            validator_result_sha256=canonical_sha256(cast(JsonValue, validator_projection)),
            final_request_sha256=final_hash,
            live_call_binding_sha256=resolved_live_policy_call_binding_sha256(binding),
            live_attempt_receipt_sha256s=live_hashes,
            live_attempt_receipt_root_sha256=live_attempt_receipt_root_sha256(attempts),
            case_execution_lease_sha256=binding.case_execution_lease_sha256,
            preflight_report_sha256=binding.preflight_report_sha256,
            factory_binding_sha256=binding.factory_binding_sha256,
            execution_authority_sha256=binding.execution_authority_sha256,
            source_transport_binding_sha256=binding.source_transport_binding_sha256,
            pricing_binding_sha256=binding.pricing_binding_sha256,
            live_openai_calls=binding.openai_calls,
            live_cost_usd_micros=binding.cost_usd_micros,
            live_cost_exact=True,
            restricted_stage_projection=restricted_stage_projection,
            restricted_stage_projection_sha256=canonical_sha256(restricted_stage_projection),
            evidence_snapshot_ns=coordinated.evidence_snapshot_latency_ns,
            history_extract_ns=history_extract_ns,
            rubric_ns=coordinated.topology_run.total_latency_ns,
            policy_ns=policy_ns,
            render_ns=render_ns,
            validator_ns=validator_ns,
            pre_provider_total_ns=max(
                pre_provider_total_ns,
                coordinated.evidence_snapshot_latency_ns,
                coordinated.topology_run.total_latency_ns,
                history_extract_ns,
                policy_ns,
                render_ns,
                validator_ns,
            ),
            _seal=_PRE_PROVIDER_SEAL,
        )
        self._admit_pre_provider(pre, sentinel_receipt_sha256=None)

    def begin_off_pre_provider(
        self,
        *,
        logical_call_id: str,
        host_id: str,
        raw_request: JsonValue,
        result: SentinelResult,
        pre_provider_total_ns: int,
    ) -> None:
        """Admit an exact no-semantic-work OFF call before actor transport."""

        if self._policy is not None:
            raise ProductionRuntimeAuditError(
                "OFF_POLICY_PRESENT", "OFF production audit cannot own live policy authority"
            )
        if type(result) is not SentinelResult:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "OFF result type differs")
        raw = _canonical_snapshot(raw_request)
        raw_hash = canonical_sha256(raw)
        receipt = result.receipt
        if (
            receipt.logical_call_id != logical_call_id
            or receipt.host_id != host_id
            or receipt.configured_mode is not SentinelMode.OFF
            or receipt.effective_mode is not SentinelMode.OFF
            or receipt.validation_status is not SentinelValidationStatus.BYPASSED
            or receipt.raw_request_sha256 != raw_hash
            or receipt.candidate_request_sha256 != raw_hash
            or receipt.final_request_sha256 != raw_hash
            or receipt.policy_evaluated
            or result.use_transformed_request
        ):
            raise ProductionRuntimeAuditError("OFF_AUDIT_INVARIANT", "OFF result differs")
        empty_diff = canonical_sha256({"diffs": [], "list_insertions": []})
        if receipt.exact_diff_sha256 != empty_diff:
            raise ProductionRuntimeAuditError("OFF_AUDIT_INVARIANT", "OFF diff is not empty")
        validator = canonical_sha256(
            cast(
                JsonValue,
                {
                    "status": "PASSED",
                    "logical_call_id": logical_call_id,
                    "host_id": host_id,
                    "configured_mode": "OFF",
                    "effective_mode": "OFF",
                    "raw_request_sha256": raw_hash,
                    "candidate_request_sha256": raw_hash,
                    "final_request_sha256": raw_hash,
                    "policy_evaluated": False,
                    "validation_checks": ["OFF_NO_SEMANTIC_WORK", "ORIGINAL_PARITY"],
                },
            )
        )
        pre = ProductionRuntimeAuditPreProviderV1(
            logical_call_id=logical_call_id,
            host_id=host_id,
            status=ProductionRuntimeAuditPreProviderStatusV1.OFF,
            outcome=ProductionRuntimeAuditPreProviderOutcomeV1.OFF,
            configured_mode=SentinelMode.OFF,
            effective_mode=SentinelMode.OFF,
            fallback_reason=None,
            fallback_check=None,
            raw_request_sha256=raw_hash,
            extraction_sha256=None,
            history_ir_sha256=None,
            codec_overlay_sha256=None,
            vertical_output_sha256=None,
            coordinated_record_sha256=None,
            rubric_result_sha256=None,
            path_relevance_output_sha256=None,
            render_result_sha256=None,
            candidate_request_sha256=raw_hash,
            exact_diff_sha256=empty_diff,
            validator_result_sha256=validator,
            final_request_sha256=raw_hash,
            live_call_binding_sha256=None,
            live_attempt_receipt_sha256s=(),
            live_attempt_receipt_root_sha256=None,
            case_execution_lease_sha256=None,
            preflight_report_sha256=None,
            factory_binding_sha256=None,
            execution_authority_sha256=None,
            source_transport_binding_sha256=None,
            pricing_binding_sha256=None,
            live_openai_calls=0,
            live_cost_usd_micros=0,
            live_cost_exact=True,
            restricted_stage_projection={
                "kind": "OFF_NO_SEMANTIC_WORK",
                "raw_request": raw,
                "final_request": raw,
                "validator_result_sha256": validator,
                "semantic_text_persisted": False,
                "reasoning_persisted": False,
            },
            restricted_stage_projection_sha256=canonical_sha256(
                {
                    "kind": "OFF_NO_SEMANTIC_WORK",
                    "raw_request": raw,
                    "final_request": raw,
                    "validator_result_sha256": validator,
                    "semantic_text_persisted": False,
                    "reasoning_persisted": False,
                }
            ),
            evidence_snapshot_ns=0,
            history_extract_ns=0,
            rubric_ns=0,
            policy_ns=0,
            render_ns=0,
            validator_ns=0,
            pre_provider_total_ns=max(0, pre_provider_total_ns),
            _seal=_PRE_PROVIDER_SEAL,
        )
        receipt_hash = canonical_sha256(cast(JsonValue, _sentinel_receipt_projection(receipt)))
        self._admit_pre_provider(pre, sentinel_receipt_sha256=receipt_hash)

    def begin_bypass_pre_provider(
        self,
        *,
        logical_call_id: str,
        host_id: str,
        raw_request: JsonValue,
        result: SentinelResult,
        pre_provider_total_ns: int,
    ) -> None:
        """Admit a live-config kill-switch bypass with exact zero semantic work."""

        if self._policy is None or type(result) is not SentinelResult:
            raise ProductionRuntimeAuditError(
                "LIVE_POLICY_MISSING", "live-config bypass requires its exact policy"
            )
        raw = _canonical_snapshot(raw_request)
        raw_hash = canonical_sha256(raw)
        receipt = result.receipt
        if (
            receipt.logical_call_id != logical_call_id
            or receipt.host_id != host_id
            or receipt.configured_mode not in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
            or receipt.effective_mode is not SentinelMode.OFF
            or receipt.validation_status is not SentinelValidationStatus.BYPASSED
            or receipt.bypass_reason is None
            or receipt.policy_evaluated
            or receipt.raw_request_sha256 != raw_hash
            or receipt.candidate_request_sha256 != raw_hash
            or receipt.final_request_sha256 != raw_hash
            or result.use_transformed_request
        ):
            raise ProductionRuntimeAuditError(
                "BYPASS_AUDIT_INVARIANT", "live-config bypass result differs"
            )
        validator_projection: JsonValue = {
            "status": "BYPASSED_ORIGINAL",
            "logical_call_id": logical_call_id,
            "host_id": host_id,
            "configured_mode": receipt.configured_mode.value,
            "effective_mode": "OFF",
            "bypass_reason": receipt.bypass_reason.value,
            "raw_request_sha256": raw_hash,
            "candidate_request_sha256": raw_hash,
            "final_request_sha256": raw_hash,
            "policy_evaluated": False,
        }
        stage_projection: JsonValue = {
            "kind": "BYPASSED_ORIGINAL",
            "raw_request": raw,
            "final_request": raw,
            "sentinel_receipt": _sentinel_receipt_projection(receipt),
            "validator_result": validator_projection,
            "provider_response_via_collector_locator": True,
            "provider_reasoning_persisted": False,
        }
        pre = ProductionRuntimeAuditPreProviderV1(
            logical_call_id=logical_call_id,
            host_id=host_id,
            status=ProductionRuntimeAuditPreProviderStatusV1.BYPASSED_ORIGINAL,
            outcome=ProductionRuntimeAuditPreProviderOutcomeV1.BYPASSED_ORIGINAL,
            configured_mode=receipt.configured_mode,
            effective_mode=SentinelMode.OFF,
            fallback_reason=None,
            fallback_check=receipt.bypass_reason.value,
            raw_request_sha256=raw_hash,
            extraction_sha256=None,
            history_ir_sha256=None,
            codec_overlay_sha256=None,
            vertical_output_sha256=None,
            coordinated_record_sha256=None,
            rubric_result_sha256=None,
            path_relevance_output_sha256=None,
            render_result_sha256=None,
            candidate_request_sha256=raw_hash,
            exact_diff_sha256=receipt.exact_diff_sha256,
            validator_result_sha256=canonical_sha256(validator_projection),
            final_request_sha256=raw_hash,
            live_call_binding_sha256=None,
            live_attempt_receipt_sha256s=(),
            live_attempt_receipt_root_sha256=None,
            case_execution_lease_sha256=None,
            preflight_report_sha256=None,
            factory_binding_sha256=None,
            execution_authority_sha256=None,
            source_transport_binding_sha256=None,
            pricing_binding_sha256=None,
            live_openai_calls=0,
            live_cost_usd_micros=0,
            live_cost_exact=True,
            restricted_stage_projection=stage_projection,
            restricted_stage_projection_sha256=canonical_sha256(stage_projection),
            evidence_snapshot_ns=0,
            history_extract_ns=0,
            rubric_ns=0,
            policy_ns=0,
            render_ns=0,
            validator_ns=0,
            pre_provider_total_ns=max(0, pre_provider_total_ns),
            _seal=_PRE_PROVIDER_SEAL,
        )
        receipt_hash = canonical_sha256(cast(JsonValue, _sentinel_receipt_projection(receipt)))
        with self._lock:
            existing = self._pending.get(logical_call_id)
            if existing is not None:
                if (
                    existing.pre_provider.status
                    is ProductionRuntimeAuditPreProviderStatusV1.BYPASSED_ORIGINAL
                    and existing.sentinel_receipt_sha256 == receipt_hash
                ):
                    return
                raise ProductionRuntimeAuditError(
                    "DUPLICATE_AUDIT_CALL", "bypass logical call repeats"
                )
        self._admit_pre_provider(pre, sentinel_receipt_sha256=receipt_hash)

    def begin_fallback_pre_provider(
        self,
        *,
        logical_call_id: str,
        host_id: str,
        raw_request: JsonValue,
        result: SentinelResult,
        fallback_check: str,
        pre_provider_total_ns: int,
    ) -> None:
        """Admit an Original fallback, including every live attempt already made."""

        if self._policy is None:
            raise ProductionRuntimeAuditError(
                "LIVE_POLICY_MISSING", "semantic fallback requires its exact live policy"
            )
        if type(result) is not SentinelResult:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "fallback result type differs")
        _require_id(fallback_check, "fallback_check", semantic=True)
        raw = _canonical_snapshot(raw_request)
        raw_hash = canonical_sha256(raw)
        receipt = result.receipt
        if (
            receipt.logical_call_id != logical_call_id
            or receipt.host_id != host_id
            or receipt.configured_mode not in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
            or receipt.effective_mode is not SentinelMode.OFF
            or receipt.validation_status is not SentinelValidationStatus.FALLBACK_ORIGINAL
            or receipt.fallback_reason is None
            or receipt.raw_request_sha256 != raw_hash
            or receipt.candidate_request_sha256 != raw_hash
            or receipt.final_request_sha256 != raw_hash
            or receipt.exact_diff_sha256 != canonical_sha256({"diffs": [], "list_insertions": []})
            or result.use_transformed_request
        ):
            raise ProductionRuntimeAuditError(
                "FALLBACK_AUDIT_INVARIANT", "fallback result differs from Original"
            )

        attempts: tuple[LiveAttemptReceiptV1, ...] = ()
        rubric_call_receipts: tuple[LiveRubricCallReceiptV1, ...] = ()
        rubric_call_trust_anchors: tuple[LiveRubricCallTrustAnchorV1, ...] = ()
        rubric_attempt_request_anchors: tuple[LiveRubricAttemptRequestAnchorV1, ...] = ()
        history_attempt_request_anchor: LiveHistoryPolicyAttemptRequestAnchorV1 | None = None
        rubric_backend_extension: R24RubricBackendExtensionDescriptorV1 | None = None
        expected_collector_stimulus_sha256: str | None = None
        expected_tracking_packet_sha256: str | None = None
        expected_history_evidence_packet_sha256: str | None = None
        actor_call_index: int | None = None
        policy_failure_code: str | None = None
        try:
            attempts = tuple(
                snapshot_live_attempt_receipt(item)
                for item in self._policy.attempt_receipts_for_call(logical_call_id)
            )
        except R24ContractError:
            # Extraction/validation can fail before the per-call live policy is
            # registered.  That is an exact zero-attempt census, not missing data.
            attempts = ()
        else:
            try:
                # Trip the one-way latch immediately, but do not call
                # ``require_clear`` yet.  The TU request proof must first be
                # cross-validated, admitted, and published in a failure (or
                # commit-recovery) terminal before this method rejects actor
                # dispatch below.
                self._run_fatal_latch.observe_attempts(
                    logical_call_id=logical_call_id,
                    attempts=attempts,
                )
            except ProductionRunFatalError as exc:
                raise ProductionRuntimeAuditError(exc.code, str(exc)) from exc
            try:
                rubric_call_receipts = tuple(
                    snapshot_live_rubric_call_receipt(item)
                    for item in self._policy.rubric_call_receipts_for_call(logical_call_id)
                )
                rubric_call_trust_anchors = self._policy.rubric_call_trust_anchors_for_call(
                    logical_call_id
                )
                rubric_attempt_request_anchors = (
                    self._policy.rubric_attempt_request_anchors_for_call(logical_call_id)
                )
                history_attempt_request_anchor = (
                    self._policy.history_policy_attempt_request_anchor_for_call(logical_call_id)
                )
                rubric_backend_extension = snapshot_r24_rubric_backend_extension_descriptor(
                    self._policy.rubric_backend_extension_descriptor()
                )
                expected_collector_stimulus_sha256 = (
                    self._policy.rubric_collector_stimulus_sha256_for_call(logical_call_id)
                )
                expected_tracking_packet_sha256 = (
                    self._policy.rubric_tracking_packet_sha256_for_call(logical_call_id)
                )
                expected_history_evidence_packet_sha256 = (
                    self._policy.history_evidence_packet_sha256_for_call(logical_call_id)
                )
                actor_call_index = self._policy.actor_call_index_for_call(logical_call_id)
                policy_failure_code = self._policy.failure_for_call(logical_call_id)
            except R24ContractError as exc:
                if attempts:
                    raise ProductionRuntimeAuditError(
                        "RUBRIC_CROSS_BINDING_MISMATCH",
                        "registered fallback call lost its rubric proof",
                    ) from exc
                rubric_call_receipts = ()
                rubric_call_trust_anchors = ()
                rubric_attempt_request_anchors = ()
                history_attempt_request_anchor = None
                rubric_backend_extension = None
                expected_collector_stimulus_sha256 = None
                expected_tracking_packet_sha256 = None
                expected_history_evidence_packet_sha256 = None
                actor_call_index = None
                policy_failure_code = None
        live_hashes = tuple(live_attempt_receipt_sha256(item) for item in attempts)

        binding: ResolvedLivePolicyCallBindingV1 | None = None
        try:
            binding = self._policy.call_binding(logical_call_id)
        except R24ContractError:
            binding = None
        try:
            validate_live_rubric_cross_bindings_v1(
                logical_call_id=logical_call_id,
                actor_request_sha256=raw_hash,
                attempts=attempts,
                rubric_attempt_request_anchors=rubric_attempt_request_anchors,
                rubric_call_receipts=rubric_call_receipts,
                rubric_call_trust_anchors=rubric_call_trust_anchors,
                expected_collector_stimulus_sha256=(expected_collector_stimulus_sha256),
                expected_tracking_packet_sha256=expected_tracking_packet_sha256,
                rubric_backend_extension=rubric_backend_extension,
                binding=binding,
                actor_call_index=(
                    binding.actor_call_index if binding is not None else actor_call_index
                ),
                expect_history_policy=(
                    binding.history_policy_attempt_receipt_sha256 is not None
                    if binding is not None
                    else (
                        None
                        if actor_call_index is None
                        else receipt.fallback_reason
                        is not SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
                    )
                ),
                allow_incomplete=True,
                history_policy_attempt_request_anchor=history_attempt_request_anchor,
            )
        except R24ContractError as exc:
            raise ProductionRuntimeAuditError(
                exc.code, "fallback rubric cross-binding differs"
            ) from exc
        known_cost = sum(item.cost_usd_micros or 0 for item in attempts)
        cost_exact = all(item.cost_usd_micros is not None for item in attempts)
        dispatched_calls = sum(item.dispatch_count for item in attempts)
        first_attempt = attempts[0] if attempts else None
        validator_projection: dict[str, JsonValue] = {
            "status": "FALLBACK_ORIGINAL",
            "logical_call_id": logical_call_id,
            "host_id": host_id,
            "configured_mode": receipt.configured_mode.value,
            "effective_mode": "OFF",
            "fallback_reason": receipt.fallback_reason.value,
            "fallback_check": fallback_check,
            "policy_evaluated": receipt.policy_evaluated,
            "raw_request_sha256": raw_hash,
            "candidate_request_sha256": raw_hash,
            "final_request_sha256": raw_hash,
            "live_attempt_count": len(attempts),
            "live_dispatch_count": dispatched_calls,
            "live_cost_usd_micros_known": known_cost,
            "live_cost_exact": cost_exact,
        }
        stage_projection: JsonValue = {
            "kind": "FALLBACK_ORIGINAL",
            "raw_request": raw,
            "final_request": raw,
            "sentinel_receipt": _sentinel_receipt_projection(receipt),
            "validator_result": validator_projection,
            "live_failure_code": policy_failure_code,
            "live_call_binding": (
                None if binding is None else resolved_live_policy_call_binding_projection(binding)
            ),
            "live_attempt_receipts": [
                cast(JsonValue, live_attempt_receipt_projection(item)) for item in attempts
            ],
            "r2_4_rubric_call_receipts": [
                cast(JsonValue, live_rubric_call_receipt_projection(item))
                for item in rubric_call_receipts
            ],
            "r2_4_rubric_request_proofs": _rubric_request_proof_detail_projection(
                rubric_attempt_request_anchors,
                attempts,
                rubric_backend_extension,
                expected_tracking_packet_sha256,
            ),
            "r2_4_history_policy_request_proof": (
                _history_request_proof_detail_projection(
                    history_attempt_request_anchor,
                    attempts,
                    expected_history_evidence_packet_sha256,
                )
            ),
            "r2_4_rubric_backend_extension": (
                None
                if rubric_backend_extension is None
                else r24_rubric_backend_extension_descriptor_projection(rubric_backend_extension)
            ),
            "raw_request_persisted_in_owner_only_detail": True,
            "provider_response_via_collector_locator": True,
            "provider_reasoning_persisted": False,
        }
        pre = ProductionRuntimeAuditPreProviderV1(
            logical_call_id=logical_call_id,
            host_id=host_id,
            status=ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL,
            outcome=ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL,
            configured_mode=receipt.configured_mode,
            effective_mode=SentinelMode.OFF,
            fallback_reason=receipt.fallback_reason,
            fallback_check=fallback_check,
            raw_request_sha256=raw_hash,
            extraction_sha256=None,
            history_ir_sha256=None,
            codec_overlay_sha256=None,
            vertical_output_sha256=None,
            coordinated_record_sha256=None,
            rubric_result_sha256=None,
            path_relevance_output_sha256=None,
            render_result_sha256=None,
            candidate_request_sha256=raw_hash,
            exact_diff_sha256=receipt.exact_diff_sha256,
            validator_result_sha256=canonical_sha256(cast(JsonValue, validator_projection)),
            final_request_sha256=raw_hash,
            live_call_binding_sha256=(
                None if binding is None else resolved_live_policy_call_binding_sha256(binding)
            ),
            live_attempt_receipt_sha256s=live_hashes,
            live_attempt_receipt_root_sha256=(
                None if not attempts else live_attempt_receipt_root_sha256(attempts)
            ),
            case_execution_lease_sha256=(
                None if first_attempt is None else first_attempt.case_execution_lease_sha256
            ),
            preflight_report_sha256=(
                None if first_attempt is None else first_attempt.preflight_sha256
            ),
            factory_binding_sha256=(None if binding is None else binding.factory_binding_sha256),
            execution_authority_sha256=(
                None
                if first_attempt is None
                else (
                    binding.execution_authority_sha256
                    if binding is not None
                    else self._policy.execution_authority_sha256
                )
            ),
            source_transport_binding_sha256=(
                None if binding is None else binding.source_transport_binding_sha256
            ),
            pricing_binding_sha256=(
                None if first_attempt is None else first_attempt.pricing_binding_sha256
            ),
            live_openai_calls=dispatched_calls,
            live_cost_usd_micros=known_cost,
            live_cost_exact=cost_exact,
            restricted_stage_projection=stage_projection,
            restricted_stage_projection_sha256=canonical_sha256(stage_projection),
            evidence_snapshot_ns=0,
            history_extract_ns=0,
            rubric_ns=sum(
                item.duration_ns for item in attempts if item.role is LiveAttemptRoleV1.RUBRIC
            ),
            policy_ns=sum(item.duration_ns for item in attempts),
            render_ns=0,
            validator_ns=0,
            pre_provider_total_ns=max(
                pre_provider_total_ns,
                sum(item.duration_ns for item in attempts),
            ),
            _seal=_PRE_PROVIDER_SEAL,
        )
        receipt_hash = canonical_sha256(cast(JsonValue, _sentinel_receipt_projection(receipt)))
        replaced: _PendingProductionAudit | None = None
        with self._lock:
            existing = self._pending.get(logical_call_id)
            if existing is not None:
                if (
                    existing.pre_provider.status
                    is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
                    and existing.sentinel_receipt_sha256 == receipt_hash
                ):
                    return
                replaced = self._pending.pop(logical_call_id)
        if replaced is not None:
            if replaced.open_attempt is not None or replaced.attempts:
                raise ProductionRuntimeAuditError(
                    "FALLBACK_AFTER_PROVIDER_FORBIDDEN",
                    "fallback cannot replace an audit after actor dispatch",
                )
            replaced.transaction.abort()
        self._admit_pre_provider(pre, sentinel_receipt_sha256=receipt_hash)
        try:
            self._observe_and_require_run_not_fatal(
                logical_call_id,
                known_attempts=attempts,
            )
        except ProductionRuntimeAuditError as exc:
            # The actor/provider gate stays closed.  First publish the admitted
            # request preimages as a zero-actor-attempt failed terminal so an
            # unreaped rubric worker cannot erase its owner-only proof.
            self.finalize_actor_failure(
                logical_call_id=logical_call_id,
                failure_phase="SENTINEL_POLICY",
                failure_code=exc.code,
            )
            raise

    def begin_no_history_pre_provider(
        self,
        *,
        logical_call_id: str,
        host_id: str,
        raw_request: JsonValue,
        result: SentinelResult,
        coordinated: R24CoordinatedCallRecordV1,
        fallback_check: str,
        pre_provider_total_ns: int,
    ) -> None:
        """Admit an Original result with complete, successful rubric evidence.

        A first actor request can validly contain no editable history while its
        independent history-free rubric still generates and tracks.  This path
        persists those admitted semantic preimages without inventing a history
        policy output or weakening the typed ``FALLBACK_ORIGINAL`` result.
        """

        if self._policy is None:
            raise ProductionRuntimeAuditError(
                "LIVE_POLICY_MISSING", "no-history rubric audit requires its exact live policy"
            )
        if (
            type(result) is not SentinelResult
            or type(coordinated) is not R24CoordinatedCallRecordV1
        ):
            raise ProductionRuntimeAuditError(
                "UNTRUSTED_TYPE", "no-history audit inputs use untrusted types"
            )
        _require_id(fallback_check, "fallback_check", semantic=True)
        raw = _canonical_snapshot(raw_request)
        raw_hash = canonical_sha256(raw)
        receipt = result.receipt
        if (
            fallback_check != "r2_4_no_history_r21_v1_compatibility"
            or receipt.logical_call_id != logical_call_id
            or receipt.host_id != host_id
            or receipt.configured_mode not in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
            or receipt.effective_mode is not SentinelMode.OFF
            or receipt.validation_status is not SentinelValidationStatus.FALLBACK_ORIGINAL
            or receipt.fallback_reason is not SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
            or receipt.policy_evaluated
            or receipt.raw_request_sha256 != raw_hash
            or receipt.candidate_request_sha256 != raw_hash
            or receipt.final_request_sha256 != raw_hash
            or receipt.exact_diff_sha256 != canonical_sha256({"diffs": [], "list_insertions": []})
            or result.use_transformed_request
        ):
            raise ProductionRuntimeAuditError(
                "NO_HISTORY_AUDIT_INVARIANT", "no-history result differs from exact Original"
            )

        # Re-read the policy-owned detached record so the seam return and the
        # durable audit cannot silently refer to different cached evaluations.
        policy_record = self._policy.coordinated_record_for_call(logical_call_id)
        coordinated_hash = r24_coordinated_call_record_sha256(coordinated)
        if r24_coordinated_call_record_sha256(policy_record) != coordinated_hash:
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "no-history coordinated records differ"
            )
        coordinated = policy_record
        relevance = coordinated.rubric_result.relevance
        if (
            coordinated.logical_call_id != logical_call_id
            or coordinated.gpt56_evidence_packet_sha256 is not None
            or coordinated.rubric_result.status is not RubricSessionStatus.ADMITTED
            or relevance is None
            or relevance.records
            or coordinated.topology_run.history_policy_input_sha256 is not None
            or coordinated.topology_run.history_policy_output_sha256 is not None
        ):
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "no-history coordinated rubric binding differs"
            )

        binding = self._policy.call_binding(logical_call_id)
        attempts = tuple(
            snapshot_live_attempt_receipt(item)
            for item in self._policy.attempt_receipts_for_call(logical_call_id)
        )
        rubric_call_receipts = tuple(
            snapshot_live_rubric_call_receipt(item)
            for item in self._policy.rubric_call_receipts_for_call(logical_call_id)
        )
        rubric_call_trust_anchors = self._policy.rubric_call_trust_anchors_for_call(logical_call_id)
        rubric_attempt_request_anchors = self._policy.rubric_attempt_request_anchors_for_call(
            logical_call_id
        )
        rubric_backend_extension = snapshot_r24_rubric_backend_extension_descriptor(
            self._policy.rubric_backend_extension_descriptor()
        )
        policy_failure_code = self._policy.failure_for_call(logical_call_id)
        try:
            validate_live_rubric_cross_bindings_v1(
                logical_call_id=logical_call_id,
                actor_request_sha256=raw_hash,
                attempts=attempts,
                rubric_attempt_request_anchors=rubric_attempt_request_anchors,
                rubric_call_receipts=rubric_call_receipts,
                rubric_call_trust_anchors=rubric_call_trust_anchors,
                expected_collector_stimulus_sha256=(coordinated.history_free_stimulus_sha256),
                expected_tracking_packet_sha256=coordinated.tracking_packet_sha256,
                rubric_backend_extension=rubric_backend_extension,
                binding=binding,
                actor_call_index=binding.actor_call_index,
                expect_history_policy=False,
                allow_incomplete=False,
            )
        except R24ContractError as exc:
            raise ProductionRuntimeAuditError(
                exc.code, "no-history rubric cross-binding differs"
            ) from exc
        live_hashes = tuple(live_attempt_receipt_sha256(item) for item in attempts)
        if (
            policy_failure_code is not None
            or binding.logical_call_id != logical_call_id
            or binding.actor_call_index != 1
            or binding.actor_request_sha256 != raw_hash
            or binding.policy_id != receipt.policy_id
            or binding.source_transport_binding_sha256 is not None
            or binding.history_policy_attempt_receipt_sha256 is not None
            or binding.output_sha256 is not None
            or binding.openai_calls != 2
            or len(attempts) != 2
            or len(rubric_call_receipts) != 2
            or live_hashes != binding.rubric_attempt_receipt_sha256s
            or tuple(live_rubric_call_receipt_sha256(item) for item in rubric_call_receipts)
            != binding.rubric_call_receipt_sha256s
            or tuple(item.attempt_receipt_sha256 for item in rubric_call_receipts) != live_hashes
            or binding.rubric_backend_extension_descriptor_sha256 != rubric_backend_extension.sha256
            or rubric_backend_extension.execution_scope
            is not LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE
            or any(
                item.backend_extension_descriptor_sha256 != rubric_backend_extension.sha256
                or item.r23_compatibility_descriptor_sha256
                != rubric_backend_extension.r23_compatibility_descriptor_sha256
                for item in rubric_call_receipts
            )
            or any(
                item.logical_call_id != logical_call_id
                or item.actor_request_sha256 != raw_hash
                or item.case_execution_lease_sha256 != binding.case_execution_lease_sha256
                or item.preflight_sha256 != binding.preflight_report_sha256
                or item.pricing_binding_sha256 != binding.pricing_binding_sha256
                or item.role is not LiveAttemptRoleV1.RUBRIC
                or item.status is not LiveAttemptStatusV1.COMPLETED
                or not item.passed
                for item in attempts
            )
        ):
            raise ProductionRuntimeAuditError(
                "INCOMPLETE_ATTEMPT_PROOF",
                "first no-history call needs two exact completed rubric attempts",
            )
        known_cost = sum(cast(int, item.cost_usd_micros) for item in attempts)
        if known_cost != binding.cost_usd_micros:
            raise ProductionRuntimeAuditError(
                "TRACE_BINDING_MISMATCH", "no-history live cost binding differs"
            )

        relevance_hash = path_relevance_output_sha256(relevance)
        rubric_result_hash = rubric_session_result_sha256(coordinated.rubric_result)
        validator_projection: dict[str, JsonValue] = {
            "status": "FALLBACK_ORIGINAL",
            "logical_call_id": logical_call_id,
            "host_id": host_id,
            "configured_mode": receipt.configured_mode.value,
            "effective_mode": "OFF",
            "fallback_reason": receipt.fallback_reason.value,
            "fallback_check": fallback_check,
            "history_policy_evaluated": False,
            "rubric_evaluated": True,
            "raw_request_sha256": raw_hash,
            "coordinated_record_sha256": coordinated_hash,
            "rubric_result_sha256": rubric_result_hash,
            "path_relevance_output_sha256": relevance_hash,
            "candidate_request_sha256": raw_hash,
            "final_request_sha256": raw_hash,
            "live_attempt_count": 2,
            "live_dispatch_count": 2,
            "live_cost_usd_micros_known": known_cost,
            "live_cost_exact": True,
            "validation_checks": [
                "NO_HISTORY_EXTRACTION",
                "LIVE_POLICY_CALL_BINDING",
                "R23_COORDINATED_RUBRIC_BINDING",
                "EXACT_ORIGINAL_FINAL_REQUEST",
            ],
        }
        stage_projection: JsonValue = {
            "kind": "NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL",
            "raw_request": raw,
            "final_request": raw,
            "sentinel_receipt": _sentinel_receipt_projection(receipt),
            "coordinated_record": r24_coordinated_call_record_projection(coordinated),
            "rubric_generation_result": _rubric_result_detail_projection(
                coordinated.generation_result
            ),
            "rubric_result": _rubric_result_detail_projection(coordinated.rubric_result),
            "path_relevance_output": path_relevance_output_projection(relevance),
            "validator_result": validator_projection,
            "live_failure_code": None,
            "live_call_binding": resolved_live_policy_call_binding_projection(binding),
            "live_attempt_receipts": [
                cast(JsonValue, live_attempt_receipt_projection(item)) for item in attempts
            ],
            "r2_4_rubric_call_receipts": [
                cast(JsonValue, live_rubric_call_receipt_projection(item))
                for item in rubric_call_receipts
            ],
            "r2_4_rubric_request_proofs": _rubric_request_proof_detail_projection(
                rubric_attempt_request_anchors,
                attempts,
                rubric_backend_extension,
                coordinated.tracking_packet_sha256,
            ),
            "r2_4_history_policy_request_proof": None,
            "r2_4_rubric_backend_extension": (
                r24_rubric_backend_extension_descriptor_projection(rubric_backend_extension)
            ),
            "semantic_stage_projections_persisted": True,
            "raw_request_persisted_in_owner_only_detail": True,
            "provider_response_via_collector_locator": True,
            "provider_reasoning_persisted": False,
        }
        pre = ProductionRuntimeAuditPreProviderV1(
            logical_call_id=logical_call_id,
            host_id=host_id,
            status=ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL,
            outcome=(
                ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL
            ),
            configured_mode=receipt.configured_mode,
            effective_mode=SentinelMode.OFF,
            fallback_reason=receipt.fallback_reason,
            fallback_check=fallback_check,
            raw_request_sha256=raw_hash,
            extraction_sha256=None,
            history_ir_sha256=None,
            codec_overlay_sha256=None,
            vertical_output_sha256=None,
            coordinated_record_sha256=coordinated_hash,
            rubric_result_sha256=rubric_result_hash,
            path_relevance_output_sha256=relevance_hash,
            render_result_sha256=None,
            candidate_request_sha256=raw_hash,
            exact_diff_sha256=receipt.exact_diff_sha256,
            validator_result_sha256=canonical_sha256(cast(JsonValue, validator_projection)),
            final_request_sha256=raw_hash,
            live_call_binding_sha256=resolved_live_policy_call_binding_sha256(binding),
            live_attempt_receipt_sha256s=live_hashes,
            live_attempt_receipt_root_sha256=live_attempt_receipt_root_sha256(attempts),
            case_execution_lease_sha256=binding.case_execution_lease_sha256,
            preflight_report_sha256=binding.preflight_report_sha256,
            factory_binding_sha256=binding.factory_binding_sha256,
            execution_authority_sha256=binding.execution_authority_sha256,
            source_transport_binding_sha256=None,
            pricing_binding_sha256=binding.pricing_binding_sha256,
            live_openai_calls=2,
            live_cost_usd_micros=known_cost,
            live_cost_exact=True,
            restricted_stage_projection=stage_projection,
            restricted_stage_projection_sha256=canonical_sha256(stage_projection),
            evidence_snapshot_ns=coordinated.evidence_snapshot_latency_ns,
            history_extract_ns=0,
            rubric_ns=coordinated.topology_run.total_latency_ns,
            policy_ns=sum(item.duration_ns for item in attempts),
            render_ns=0,
            validator_ns=0,
            pre_provider_total_ns=max(
                pre_provider_total_ns,
                coordinated.evidence_snapshot_latency_ns,
                coordinated.topology_run.total_latency_ns,
                sum(item.duration_ns for item in attempts),
            ),
            _seal=_PRE_PROVIDER_SEAL,
        )
        receipt_hash = canonical_sha256(cast(JsonValue, _sentinel_receipt_projection(receipt)))
        with self._lock:
            existing = self._pending.get(logical_call_id)
            if existing is not None:
                if (
                    existing.pre_provider.status
                    is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
                    and existing.pre_provider.coordinated_record_sha256 == coordinated_hash
                    and existing.sentinel_receipt_sha256 == receipt_hash
                ):
                    return
                raise ProductionRuntimeAuditError(
                    "DUPLICATE_AUDIT_CALL", "no-history logical call repeats"
                )
        self._admit_pre_provider(pre, sentinel_receipt_sha256=receipt_hash)

    def _admit_pre_provider(
        self,
        pre: ProductionRuntimeAuditPreProviderV1,
        *,
        sentinel_receipt_sha256: str | None,
    ) -> None:
        # Retain a private detached recovery preimage before the sink sees a
        # second detached copy.  A failing/malicious sink therefore cannot
        # mutate nested request-proof data and erase the recovery record.
        pre = _snapshot_production_runtime_audit_pre_provider(pre)
        sink_pre = _snapshot_production_runtime_audit_pre_provider(pre)
        logical_call_id = pre.logical_call_id
        pre_provider_sha256 = production_runtime_audit_pre_provider_sha256(pre)
        with self._lock:
            if (
                logical_call_id in self._pending
                or logical_call_id in self._completed
                or logical_call_id in self._failures
                or logical_call_id in self._commit_failures
                or logical_call_id in self._admission_failures
            ):
                raise ProductionRuntimeAuditError(
                    "DUPLICATE_AUDIT_CALL", "logical call repeats before admission"
                )

        try:
            transaction = self._sink_begin(sink_pre)
        except Exception as exc:
            if type(exc) is ProductionRuntimeAuditSinkAdmissionError:
                admission_stage = exc.admission_stage
                sink_exception_type = exc.sink_exception_type
            else:
                admission_stage = ProductionRuntimeAuditAdmissionStageV1.SINK_BEGIN
                sink_exception_type = _exception_type_label(exc)
            recovery = ProductionRuntimeAuditAdmissionFailureReceiptV1(
                logical_call_id=logical_call_id,
                publication_status=(
                    ProductionRuntimeAuditPublicationStatusV1.ADMISSION_OUTCOME_UNKNOWN
                ),
                failure_phase="AUDIT_PRE_PROVIDER_ADMISSION",
                failure_code="AUDIT_PRE_PROVIDER_ADMISSION_FAILED",
                admission_stage=admission_stage,
                sink_exception_type=sink_exception_type,
                pre_provider=pre,
                pre_provider_sha256=pre_provider_sha256,
                sentinel_receipt_sha256=sentinel_receipt_sha256,
                _seal=_ADMISSION_FAILURE_RECEIPT_SEAL,
            )
            self._record_admission_failure(recovery)
            raise ProductionRuntimeAuditError(
                "AUDIT_PRE_PROVIDER_ADMISSION_FAILED",
                "production audit pre-provider admission outcome is unknown",
            ) from exc

        try:
            transaction_matches = (
                isinstance(  # type: ignore[redundant-expr]
                    transaction, ProductionRuntimeAuditTransactionV1
                )
                and transaction.logical_call_id == logical_call_id
                and transaction.pre_provider_sha256 == pre_provider_sha256
            )
        except Exception:
            transaction_matches = False
        if not transaction_matches:
            try:
                transaction.abort()
            except Exception:
                pass
            recovery = ProductionRuntimeAuditAdmissionFailureReceiptV1(
                logical_call_id=logical_call_id,
                publication_status=(
                    ProductionRuntimeAuditPublicationStatusV1.ADMISSION_OUTCOME_UNKNOWN
                ),
                failure_phase="AUDIT_PRE_PROVIDER_ADMISSION",
                failure_code="AUDIT_PRE_PROVIDER_ADMISSION_FAILED",
                admission_stage=(ProductionRuntimeAuditAdmissionStageV1.TRANSACTION_BINDING),
                sink_exception_type="SinkAdmissionMismatch",
                pre_provider=pre,
                pre_provider_sha256=pre_provider_sha256,
                sentinel_receipt_sha256=sentinel_receipt_sha256,
                _seal=_ADMISSION_FAILURE_RECEIPT_SEAL,
            )
            self._record_admission_failure(recovery)
            raise ProductionRuntimeAuditError(
                "AUDIT_PRE_PROVIDER_ADMISSION_FAILED",
                "production audit sink transaction differs",
            )
        pending = _PendingProductionAudit(
            pre_provider=pre,
            transaction=transaction,
            sentinel_receipt_sha256=sentinel_receipt_sha256,
            attempts=[],
            open_attempt=None,
            normalized_actor_output_sha256=None,
            parser_input_sha256=None,
            parser_id=None,
            parser_status=None,
            parser_attempt_count=None,
            parsed_action=None,
            parsed_action_sha256=None,
            parser_ns=None,
        )
        with self._lock:
            if (
                logical_call_id in self._pending
                or logical_call_id in self._completed
                or logical_call_id in self._failures
                or logical_call_id in self._commit_failures
                or logical_call_id in self._admission_failures
            ):
                transaction.abort()
                raise ProductionRuntimeAuditError("DUPLICATE_AUDIT_CALL", "logical call repeats")
            self._pending[logical_call_id] = pending

    @staticmethod
    def _validate_live_bindings(
        *,
        logical_call_id: str,
        raw_request_sha256: str,
        policy_output: RuntimeVerticalPolicyOutputV1,
        binding: ResolvedLivePolicyCallBindingV1,
        attempts: tuple[LiveAttemptReceiptV1, ...],
        rubric_attempt_request_anchors: tuple[LiveRubricAttemptRequestAnchorV1, ...],
        history_attempt_request_anchor: LiveHistoryPolicyAttemptRequestAnchorV1 | None,
        rubric_call_receipts: tuple[LiveRubricCallReceiptV1, ...],
        rubric_call_trust_anchors: tuple[LiveRubricCallTrustAnchorV1, ...],
        rubric_backend_extension: R24RubricBackendExtensionDescriptorV1,
        coordinated: R24CoordinatedCallRecordV1,
    ) -> None:
        if (
            type(binding) is not ResolvedLivePolicyCallBindingV1
            or type(coordinated) is not R24CoordinatedCallRecordV1
        ):
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "live binding type differs")
        try:
            validate_live_rubric_cross_bindings_v1(
                logical_call_id=logical_call_id,
                actor_request_sha256=raw_request_sha256,
                attempts=attempts,
                rubric_attempt_request_anchors=rubric_attempt_request_anchors,
                rubric_call_receipts=rubric_call_receipts,
                rubric_call_trust_anchors=rubric_call_trust_anchors,
                expected_collector_stimulus_sha256=(coordinated.history_free_stimulus_sha256),
                expected_tracking_packet_sha256=coordinated.tracking_packet_sha256,
                rubric_backend_extension=rubric_backend_extension,
                binding=binding,
                actor_call_index=binding.actor_call_index,
                expect_history_policy=True,
                allow_incomplete=False,
                history_policy_attempt_request_anchor=history_attempt_request_anchor,
            )
        except R24ContractError as exc:
            raise ProductionRuntimeAuditError(
                exc.code, "live rubric cross-binding differs"
            ) from exc
        resolved_live_policy_call_binding_projection(binding)
        r24_coordinated_call_record_projection(coordinated)
        trusted_rubric_extension = snapshot_r24_rubric_backend_extension_descriptor(
            rubric_backend_extension
        )
        if (
            binding.logical_call_id != logical_call_id
            or binding.actor_request_sha256 != raw_request_sha256
            or binding.output_sha256 != vertical_output_sha256(policy_output)
            or binding.policy_id != policy_output.policy_id
            or binding.execution_authority_sha256 != policy_output.execution_authority_sha256
            or binding.source_transport_binding_sha256
            != policy_output.source_transport_binding_sha256
            or coordinated.logical_call_id != logical_call_id
            or coordinated.rubric_result.status is not RubricSessionStatus.ADMITTED
            or coordinated.rubric_result.relevance is None
            or binding.rubric_backend_extension_descriptor_sha256 != trusted_rubric_extension.sha256
            or trusted_rubric_extension.execution_scope
            is not LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE
        ):
            raise ProductionRuntimeAuditError("TRACE_BINDING_MISMATCH", "live call binding differs")
        if type(attempts) is not tuple or len(attempts) != binding.openai_calls:
            raise ProductionRuntimeAuditError(
                "INVALID_ATTEMPT_CENSUS", "live attempt count differs"
            )
        trusted = tuple(snapshot_live_attempt_receipt(item) for item in attempts)
        if any(
            item.logical_call_id != logical_call_id
            or item.actor_request_sha256 != raw_request_sha256
            or item.case_execution_lease_sha256 != binding.case_execution_lease_sha256
            or item.status is not LiveAttemptStatusV1.COMPLETED
            or not item.passed
            for item in trusted
        ):
            raise ProductionRuntimeAuditError(
                "INCOMPLETE_ATTEMPT_PROOF", "live attempt is incomplete"
            )
        rubric = tuple(
            live_attempt_receipt_sha256(item)
            for item in trusted
            if item.role is LiveAttemptRoleV1.RUBRIC
        )
        history = tuple(
            live_attempt_receipt_sha256(item)
            for item in trusted
            if item.role is LiveAttemptRoleV1.HISTORY_POLICY
        )
        if rubric != binding.rubric_attempt_receipt_sha256s or history != (
            binding.history_policy_attempt_receipt_sha256,
        ):
            raise ProductionRuntimeAuditError("INVALID_ATTEMPT_CENSUS", "live role roots differ")
        trusted_rubric_calls = tuple(
            snapshot_live_rubric_call_receipt(item) for item in rubric_call_receipts
        )
        if (
            tuple(live_rubric_call_receipt_sha256(item) for item in trusted_rubric_calls)
            != binding.rubric_call_receipt_sha256s
            or tuple(item.attempt_receipt_sha256 for item in trusted_rubric_calls) != rubric
            or any(
                item.logical_call_id != logical_call_id
                or item.execution_scope is not LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE
                or item.backend_extension_descriptor_sha256 != trusted_rubric_extension.sha256
                or item.r23_compatibility_descriptor_sha256
                != trusted_rubric_extension.r23_compatibility_descriptor_sha256
                for item in trusted_rubric_calls
            )
        ):
            raise ProductionRuntimeAuditError(
                "RUBRIC_RECEIPT_BINDING_MISMATCH",
                "R2.4 rubric receipts differ from provider attempts",
            )

    def bind_actor_sdk_arguments(
        self,
        *,
        logical_call_id: str,
        result: SentinelResult | RuntimeVerticalSentinelResultV1,
        sdk_arguments: JsonValue,
        collector_request_locator: JsonValue,
        stream: bool,
    ) -> str:
        """Bind the exact kwargs immediately before ``create`` is invoked."""

        _require_id(logical_call_id, "logical_call_id")
        self._observe_and_require_run_not_fatal(logical_call_id)
        if type(stream) is not bool:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "stream flag is invalid")
        if stream:
            raise ProductionRuntimeAuditError(
                "STREAMING_ACTOR_AUDIT_UNSUPPORTED",
                "production Qwen/MAI audit requires a complete non-stream response",
            )
        if type(result) not in {SentinelResult, RuntimeVerticalSentinelResultV1}:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "Sentinel result type differs")
        arguments = _canonical_snapshot(sdk_arguments)
        arguments_hash = canonical_sha256(arguments)
        request_locator = _collector_artifact_locator_projection(
            collector_request_locator,
            expected_event_types=frozenset({"model_request"}),
        )
        with self._lock:
            pending = self._pending.get(logical_call_id)
            if pending is None:
                raise ProductionRuntimeAuditError(
                    "PRE_PROVIDER_STAGE_MISSING", "audit begin is absent"
                )
            if pending.open_attempt is not None:
                raise ProductionRuntimeAuditError(
                    "ATTEMPT_ALREADY_OPEN", "provider attempt overlaps"
                )
            receipt = result.receipt
            receipt_projection = _sentinel_receipt_projection(receipt)
            receipt_hash = canonical_sha256(cast(JsonValue, receipt_projection))
            pre = pending.pre_provider
            common_mismatch = (
                receipt.logical_call_id != logical_call_id
                or receipt.raw_request_sha256 != pre.raw_request_sha256
                or receipt.final_request_sha256 != pre.final_request_sha256
                or receipt.candidate_request_sha256 != pre.candidate_request_sha256
                or receipt.exact_diff_sha256 != pre.exact_diff_sha256
                or arguments_hash != pre.final_request_sha256
            )
            if pre.status is ProductionRuntimeAuditPreProviderStatusV1.OFF:
                mode_mismatch = (
                    type(result) is not SentinelResult
                    or receipt.validation_status is not SentinelValidationStatus.BYPASSED
                    or receipt.policy_evaluated
                    or pre.vertical_output_sha256 is not None
                    or pre.codec_overlay_sha256 is not None
                )
            elif pre.status is ProductionRuntimeAuditPreProviderStatusV1.BYPASSED_ORIGINAL:
                mode_mismatch = (
                    type(result) is not SentinelResult
                    or receipt.validation_status is not SentinelValidationStatus.BYPASSED
                    or receipt.effective_mode is not SentinelMode.OFF
                    or receipt.configured_mode is not pre.configured_mode
                    or receipt.bypass_reason is None
                    or receipt.bypass_reason.value != pre.fallback_check
                    or receipt.policy_evaluated
                    or result.use_transformed_request
                )
            elif pre.status is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL:
                mode_mismatch = (
                    receipt.validation_status is not SentinelValidationStatus.FALLBACK_ORIGINAL
                    or receipt.effective_mode is not SentinelMode.OFF
                    or receipt.configured_mode is not pre.configured_mode
                    or receipt.fallback_reason is not pre.fallback_reason
                    or tuple(receipt.validation_checks) != (pre.fallback_check,)
                    or result.use_transformed_request
                )
            else:
                mode_mismatch = (
                    type(result) is not RuntimeVerticalSentinelResultV1
                    or receipt.validation_status is not SentinelValidationStatus.PASSED
                    or receipt.policy_output_sha256 != pre.vertical_output_sha256
                    or result.overlay_declaration_sha256 != pre.codec_overlay_sha256
                )
            if common_mismatch or mode_mismatch:
                raise ProductionRuntimeAuditError(
                    "PROVIDER_FINAL_REQUEST_MISMATCH",
                    "actual SDK arguments differ from validated Sentinel final request",
                )
            if pending.sentinel_receipt_sha256 is None:
                pending.sentinel_receipt_sha256 = receipt_hash
            elif pending.sentinel_receipt_sha256 != receipt_hash:
                raise ProductionRuntimeAuditError(
                    "SENTINEL_RECEIPT_DRIFT", "receipt changed on retry"
                )
            attempt_index = len(pending.attempts) + 1
            attempt_seed = f"{logical_call_id}:{attempt_index}".encode()
            attempt_id = f"r24-actor-{sha256(attempt_seed).hexdigest()[:32]}"
            pending.open_attempt = _OpenProviderAttempt(
                attempt_id=attempt_id,
                attempt_index=attempt_index,
                sdk_arguments_sha256=arguments_hash,
                collector_request_locator=request_locator,
                started_ns=monotonic_ns(),
            )
            return attempt_id

    @staticmethod
    def _provider_response_sha256(value: Any) -> str:
        try:
            if type(value) in {dict, list, str, int, float, bool} or value is None:
                projection = _canonical_snapshot(cast(JsonValue, value))
            else:
                model_dump = getattr(value, "model_dump", None)
                if not callable(model_dump):
                    raise TypeError("provider response has no canonical SDK projection")
                projection = _canonical_snapshot(cast(JsonValue, model_dump(mode="json")))
            return canonical_sha256(projection)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ProductionRuntimeAuditError(
                "PROVIDER_RESPONSE_HASH_FAILED", "provider response could not be canonically hashed"
            ) from exc

    def record_actor_provider_attempt(
        self,
        *,
        logical_call_id: str,
        succeeded: bool,
        latency_ns: int,
        collector_terminal_locator: JsonValue,
        raw_response: Any = None,
        response_id: str | None = None,
        model_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> ProductionActorProviderAttemptV1:
        if type(latency_ns) is not int or latency_ns < 0:
            raise ProductionRuntimeAuditError(
                "INVALID_PROVIDER_METADATA", "provider metadata differs"
            )
        terminal_locator = _collector_artifact_locator_projection(
            collector_terminal_locator,
            expected_event_types=frozenset({"model_response", "model_attempt_failed"}),
        )
        with self._lock:
            pending = self._pending.get(logical_call_id)
            if pending is None or pending.open_attempt is None:
                raise ProductionRuntimeAuditError(
                    "ATTEMPT_NOT_OPEN", "provider attempt begin is absent"
                )
            opened = pending.open_attempt
            pending.open_attempt = None
            actual_latency = max(latency_ns, monotonic_ns() - opened.started_ns)
            response_hash = self._provider_response_sha256(raw_response) if succeeded else None
            attempt = ProductionActorProviderAttemptV1(
                attempt_id=opened.attempt_id,
                attempt_index=opened.attempt_index,
                sdk_arguments_sha256=opened.sdk_arguments_sha256,
                final_request_sha256=pending.pre_provider.final_request_sha256,
                collector_request_locator=opened.collector_request_locator,
                collector_terminal_locator=terminal_locator,
                status=(
                    ProductionActorProviderAttemptStatusV1.SUCCEEDED
                    if succeeded
                    else ProductionActorProviderAttemptStatusV1.FAILED
                ),
                provider_response_sha256=response_hash,
                response_id_sha256=None if response_id is None else canonical_sha256(response_id),
                model_id_sha256=None if model_id is None else canonical_sha256(model_id),
                finish_reason=finish_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_ns=actual_latency,
                failure_code=None if succeeded else "PROVIDER_EXCEPTION",
            )
            pending.attempts.append(attempt)
            return attempt

    def finalize_actor_output(
        self,
        *,
        logical_call_id: str,
        raw_provider_response: JsonValue,
        raw_parser_input: JsonValue,
        parsed_action: JsonValue,
        parser_id: str,
        parser_status: ParserResultStatusV1,
        parser_attempt_count: int,
        parser_ns: int,
        **safe_provider_metadata: object,
    ) -> None:
        """Bind parser output, retaining no provider or parser content."""

        del safe_provider_metadata
        normalized_hash = canonical_sha256(_canonical_snapshot(raw_provider_response))
        parser_input_hash = canonical_sha256(_canonical_snapshot(raw_parser_input))
        action = _canonical_snapshot(parsed_action)
        _reject_reasoning_keys(action)
        action_hash = canonical_sha256(action)
        _require_id(parser_id, "parser_id", semantic=True)
        if type(parser_status) is not ParserResultStatusV1:
            raise ProductionRuntimeAuditError("UNTRUSTED_TYPE", "parser status differs")
        if type(parser_attempt_count) is not int or parser_attempt_count < 1:
            raise ProductionRuntimeAuditError("INVALID_PARSER_METADATA", "parser count differs")
        if type(parser_ns) is not int or parser_ns < 0:
            raise ProductionRuntimeAuditError("INVALID_LATENCY", "parser latency differs")
        with self._lock:
            pending = self._pending.get(logical_call_id)
            if pending is None:
                raise ProductionRuntimeAuditError(
                    "PRE_PROVIDER_STAGE_MISSING", "audit begin is absent"
                )
            if pending.open_attempt is not None:
                raise ProductionRuntimeAuditError(
                    "ATTEMPT_STILL_OPEN", "provider attempt is incomplete"
                )
            if (
                not pending.attempts
                or pending.attempts[-1].status
                is not ProductionActorProviderAttemptStatusV1.SUCCEEDED
            ):
                raise ProductionRuntimeAuditError(
                    "SUCCESSFUL_PROVIDER_ATTEMPT_MISSING", "no final response"
                )
            if pending.parsed_action is not None:
                raise ProductionRuntimeAuditError(
                    "PARSER_ALREADY_FINALIZED", "parser terminal repeats"
                )
            pending.normalized_actor_output_sha256 = normalized_hash
            pending.parser_input_sha256 = parser_input_hash
            pending.parser_id = parser_id
            pending.parser_status = parser_status
            pending.parser_attempt_count = parser_attempt_count
            pending.parsed_action = action
            pending.parsed_action_sha256 = action_hash
            pending.parser_ns = parser_ns

    def finalize_action_execution(
        self,
        *,
        logical_call_id: str,
        parsed_action: JsonValue,
        action_executed: bool,
        action_execution_ns: int = 0,
    ) -> ProductionRuntimeAuditReceiptV1:
        """Publish a terminal detail after the driver decides action execution."""

        if type(action_execution_ns) is not int or action_execution_ns < 0:
            raise ProductionRuntimeAuditError("INVALID_ACTION_METADATA", "action metadata differs")
        action = _canonical_snapshot(parsed_action)
        action_hash = canonical_sha256(action)
        with self._lock:
            pending = self._pending.pop(logical_call_id, None)
        if pending is None:
            raise ProductionRuntimeAuditError(
                "PARSER_STAGE_MISSING", "pending parser result is absent"
            )
        try:
            if (
                pending.parsed_action is None
                or pending.parsed_action_sha256 != action_hash
                or pending.sentinel_receipt_sha256 is None
                or pending.normalized_actor_output_sha256 is None
                or pending.parser_input_sha256 is None
                or pending.parser_id is None
                or pending.parser_status is None
                or pending.parser_attempt_count is None
                or pending.parser_ns is None
            ):
                raise ProductionRuntimeAuditError(
                    "ACTION_BINDING_MISMATCH", "parser/action proof differs"
                )
            attempts = tuple(pending.attempts)
            successful = attempts[-1]
            assert successful.provider_response_sha256 is not None
            attempt_hashes = [
                canonical_sha256(
                    cast(JsonValue, production_actor_provider_attempt_projection(item))
                )
                for item in attempts
            ]
            attempt_root = canonical_sha256(
                cast(
                    JsonValue,
                    {
                        "schema_version": "mobileworld.runtime.sentinel-r2.4-production-actor-attempt-root/v1",
                        "attempt_sha256s": attempt_hashes,
                    },
                )
            )
            provider_total_ns = sum(item.latency_ns for item in attempts)
            parser_result_projection: dict[str, JsonValue] = {
                "parser_id": pending.parser_id,
                "status": pending.parser_status.value,
                "attempt_count": pending.parser_attempt_count,
                "normalized_actor_output_sha256": pending.normalized_actor_output_sha256,
                "parser_input_sha256": pending.parser_input_sha256,
                "parsed_action_sha256": action_hash,
                "parser_input_persisted": False,
                "reasoning_persisted": False,
            }
            detail_seed = sha256(logical_call_id.encode("utf-8")).hexdigest()[:32]
            detail = ProductionRuntimeAuditDetailV1(
                detail_id=f"r24-production-detail-{detail_seed}",
                logical_call_id=logical_call_id,
                pre_provider=pending.pre_provider,
                pre_provider_sha256=production_runtime_audit_pre_provider_sha256(
                    pending.pre_provider
                ),
                sentinel_receipt_sha256=pending.sentinel_receipt_sha256,
                actor_provider_attempts=attempts,
                actor_provider_attempt_root_sha256=attempt_root,
                successful_provider_response_sha256=successful.provider_response_sha256,
                normalized_actor_output_sha256=pending.normalized_actor_output_sha256,
                parser_input_sha256=pending.parser_input_sha256,
                parser_id=pending.parser_id,
                parser_status=pending.parser_status,
                parser_attempt_count=pending.parser_attempt_count,
                parsed_action=copy_json(action),
                parsed_action_sha256=action_hash,
                action_executed=action_executed,
                executed_action_sha256=action_hash if action_executed else None,
                provider_total_ns=provider_total_ns,
                parser_ns=pending.parser_ns,
                action_execution_ns=action_execution_ns,
                total_ns=(
                    pending.pre_provider.pre_provider_total_ns
                    + provider_total_ns
                    + pending.parser_ns
                    + action_execution_ns
                ),
                _seal=_DETAIL_SEAL,
            )
            detail_hash = production_runtime_audit_detail_sha256(detail)
            receipt = ProductionRuntimeAuditReceiptV1(
                detail_id=detail.detail_id,
                logical_call_id=logical_call_id,
                raw_request_sha256=pending.pre_provider.raw_request_sha256,
                final_request_sha256=pending.pre_provider.final_request_sha256,
                provider_request_sha256=successful.sdk_arguments_sha256,
                provider_response_sha256=successful.provider_response_sha256,
                exact_diff_sha256=pending.pre_provider.exact_diff_sha256,
                pre_provider_sha256=detail.pre_provider_sha256,
                pre_provider_status=pending.pre_provider.status,
                pre_provider_outcome=pending.pre_provider.outcome,
                fallback_reason=pending.pre_provider.fallback_reason,
                fallback_check=pending.pre_provider.fallback_check,
                live_call_binding_sha256=pending.pre_provider.live_call_binding_sha256,
                live_attempt_receipt_root_sha256=(
                    pending.pre_provider.live_attempt_receipt_root_sha256
                ),
                actor_provider_attempt_root_sha256=attempt_root,
                sentinel_receipt_sha256=pending.sentinel_receipt_sha256,
                parser_input_sha256=pending.parser_input_sha256,
                parser_result_sha256=canonical_sha256(cast(JsonValue, parser_result_projection)),
                parsed_action_sha256=action_hash,
                action_executed=action_executed,
                executed_action_sha256=action_hash if action_executed else None,
                provider_attempt_count=len(attempts),
                live_openai_calls=pending.pre_provider.live_openai_calls,
                live_cost_usd_micros=pending.pre_provider.live_cost_usd_micros,
                live_cost_exact=pending.pre_provider.live_cost_exact,
                total_ns=detail.total_ns,
                detail_sha256=detail_hash,
                _seal=_RECEIPT_SEAL,
            )
            recovery_receipt = ProductionRuntimeAuditCommitFailureReceiptV1(
                logical_call_id=logical_call_id,
                terminal_kind=ProductionRuntimeAuditTerminalKindV1.ACTION_EXECUTION,
                publication_status=(
                    ProductionRuntimeAuditPublicationStatusV1.COMMIT_OUTCOME_UNKNOWN
                ),
                failure_phase="AUDIT_TERMINAL_COMMIT",
                failure_code="AUDIT_TERMINAL_COMMIT_FAILED",
                attempted_terminal_receipt=receipt,
                attempted_terminal_receipt_sha256=production_runtime_audit_receipt_sha256(receipt),
                pre_provider=pending.pre_provider,
                actor_provider_attempts=attempts,
                parsed_action=copy_json(action),
                _seal=_COMMIT_FAILURE_RECEIPT_SEAL,
            )
        except Exception:
            try:
                pending.transaction.abort()
            except Exception:
                pass
            raise
        try:
            pending.transaction.commit(detail)
        except Exception as exc:
            self._record_commit_failure(recovery_receipt)
            try:
                pending.transaction.abort()
            except Exception:
                pass
            raise ProductionRuntimeAuditError(
                "AUDIT_TERMINAL_COMMIT_FAILED",
                "production audit terminal publication outcome is unknown",
            ) from exc
        with self._lock:
            if (
                logical_call_id in self._completed
                or logical_call_id in self._failures
                or logical_call_id in self._commit_failures
                or logical_call_id in self._admission_failures
            ):
                raise ProductionRuntimeAuditError(
                    "DUPLICATE_AUDIT_CALL", "terminal receipt repeats"
                )
            self._completed[logical_call_id] = receipt
            self._completed_order.append(logical_call_id)
        return receipt

    def finalize_actor_failure(
        self,
        *,
        logical_call_id: str,
        failure_phase: str,
        failure_code: str,
    ) -> ProductionRuntimeAuditFailureReceiptV1:
        """Persist a terminal failed actor call with all incurred attempt/cost evidence."""

        _require_id(logical_call_id, "logical_call_id")
        _require_id(failure_phase, "failure_phase", semantic=True)
        _require_id(failure_code, "failure_code", semantic=True)
        with self._lock:
            pending = self._pending.pop(logical_call_id, None)
        if pending is None:
            raise ProductionRuntimeAuditError(
                "PRE_PROVIDER_STAGE_MISSING", "failed audit begin is absent"
            )
        try:
            if pending.open_attempt is not None:
                raise ProductionRuntimeAuditError(
                    "ATTEMPT_STILL_OPEN", "failed provider attempt is not terminal"
                )
            if pending.sentinel_receipt_sha256 is None:
                raise ProductionRuntimeAuditError(
                    "SENTINEL_RECEIPT_MISSING", "failed call has no Sentinel receipt"
                )
            attempts = tuple(pending.attempts)
            attempt_hashes = tuple(
                canonical_sha256(
                    cast(JsonValue, production_actor_provider_attempt_projection(item))
                )
                for item in attempts
            )
            attempt_root = canonical_sha256(
                cast(
                    JsonValue,
                    {
                        "attempt_sha256s": list(attempt_hashes),
                        "schema_version": "mobileworld.runtime.sentinel-r2.4-production-actor-attempt-root/v1",
                    },
                )
            )
            pre_hash = production_runtime_audit_pre_provider_sha256(pending.pre_provider)
            total_ns = pending.pre_provider.pre_provider_total_ns + sum(
                item.latency_ns for item in attempts
            )
            detail: JsonValue = {
                "actor_provider_attempt_root_sha256": attempt_root,
                "actor_provider_attempts": [
                    production_actor_provider_attempt_projection(item) for item in attempts
                ],
                "failure_code": failure_code,
                "failure_phase": failure_phase,
                "logical_call_id": logical_call_id,
                "pre_provider": production_runtime_audit_pre_provider_projection(
                    pending.pre_provider
                ),
                "pre_provider_sha256": pre_hash,
                "schema_version": PRODUCTION_RUNTIME_AUDIT_FAILURE_SCHEMA_VERSION,
                "sentinel_receipt_sha256": pending.sentinel_receipt_sha256,
                "status": "FAILED",
                "total_ns": total_ns,
            }
            detail_hash = canonical_sha256(detail)
            receipt = ProductionRuntimeAuditFailureReceiptV1(
                logical_call_id=logical_call_id,
                raw_request_sha256=pending.pre_provider.raw_request_sha256,
                final_request_sha256=pending.pre_provider.final_request_sha256,
                pre_provider_sha256=pre_hash,
                actor_provider_attempt_root_sha256=attempt_root,
                sentinel_receipt_sha256=pending.sentinel_receipt_sha256,
                provider_attempt_count=len(attempts),
                live_openai_calls=pending.pre_provider.live_openai_calls,
                live_cost_usd_micros=pending.pre_provider.live_cost_usd_micros,
                live_cost_exact=pending.pre_provider.live_cost_exact,
                failure_phase=failure_phase,
                failure_code=failure_code,
                total_ns=total_ns,
                detail_sha256=detail_hash,
                _seal=_FAILURE_RECEIPT_SEAL,
            )
            recovery_receipt = ProductionRuntimeAuditCommitFailureReceiptV1(
                logical_call_id=logical_call_id,
                terminal_kind=ProductionRuntimeAuditTerminalKindV1.ACTOR_FAILURE,
                publication_status=(
                    ProductionRuntimeAuditPublicationStatusV1.COMMIT_OUTCOME_UNKNOWN
                ),
                failure_phase="AUDIT_TERMINAL_COMMIT",
                failure_code="AUDIT_TERMINAL_COMMIT_FAILED",
                attempted_terminal_receipt=receipt,
                attempted_terminal_receipt_sha256=(
                    production_runtime_audit_failure_receipt_sha256(receipt)
                ),
                pre_provider=pending.pre_provider,
                actor_provider_attempts=attempts,
                parsed_action=None,
                _seal=_COMMIT_FAILURE_RECEIPT_SEAL,
            )
        except Exception:
            try:
                pending.transaction.abort()
            except Exception:
                pass
            raise
        try:
            pending.transaction.commit_failure(detail)
        except Exception as exc:
            self._record_commit_failure(recovery_receipt)
            try:
                pending.transaction.abort()
            except Exception:
                pass
            raise ProductionRuntimeAuditError(
                "AUDIT_TERMINAL_COMMIT_FAILED",
                "production audit failed-terminal publication outcome is unknown",
            ) from exc
        with self._lock:
            if (
                logical_call_id in self._completed
                or logical_call_id in self._failures
                or logical_call_id in self._commit_failures
                or logical_call_id in self._admission_failures
            ):
                raise ProductionRuntimeAuditError(
                    "DUPLICATE_AUDIT_CALL", "terminal failed receipt repeats"
                )
            self._failures[logical_call_id] = receipt
            self._failure_order.append(logical_call_id)
        return receipt

    def cancel(self, logical_call_id: str) -> None:
        if type(logical_call_id) is not str:
            return
        with self._lock:
            pending = self._pending.pop(logical_call_id, None)
        if pending is not None:
            pending.transaction.abort()

    def receipt_for(self, logical_call_id: str) -> ProductionRuntimeAuditReceiptV1:
        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            receipt = self._completed.get(logical_call_id)
        if receipt is None:
            raise ProductionRuntimeAuditError("AUDIT_RECEIPT_UNAVAILABLE", "receipt is absent")
        return receipt

    def failure_receipt_for(self, logical_call_id: str) -> ProductionRuntimeAuditFailureReceiptV1:
        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            receipt = self._failures.get(logical_call_id)
        if receipt is None:
            raise ProductionRuntimeAuditError(
                "AUDIT_FAILURE_RECEIPT_UNAVAILABLE", "failure receipt is absent"
            )
        return receipt

    def commit_failure_receipt_for(
        self, logical_call_id: str
    ) -> ProductionRuntimeAuditCommitFailureReceiptV1:
        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            receipt = self._commit_failures.get(logical_call_id)
        if receipt is None:
            raise ProductionRuntimeAuditError(
                "AUDIT_COMMIT_FAILURE_RECEIPT_UNAVAILABLE",
                "commit-failure recovery receipt is absent",
            )
        return _snapshot_production_runtime_audit_commit_failure_receipt(receipt)

    def admission_failure_receipt_for(
        self, logical_call_id: str
    ) -> ProductionRuntimeAuditAdmissionFailureReceiptV1:
        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            receipt = self._admission_failures.get(logical_call_id)
        if receipt is None:
            raise ProductionRuntimeAuditError(
                "AUDIT_ADMISSION_FAILURE_RECEIPT_UNAVAILABLE",
                "admission-failure recovery receipt is absent",
            )
        return _snapshot_production_runtime_audit_admission_failure_receipt(receipt)

    @property
    def latest_failure_receipt(self) -> ProductionRuntimeAuditFailureReceiptV1 | None:
        with self._lock:
            if not self._failure_order:
                return None
            return self._failures[self._failure_order[-1]]

    @property
    def latest_completed_receipt(self) -> ProductionRuntimeAuditReceiptV1 | None:
        with self._lock:
            if not self._completed_order:
                return None
            return self._completed[self._completed_order[-1]]

    @property
    def latest_commit_failure_receipt(
        self,
    ) -> ProductionRuntimeAuditCommitFailureReceiptV1 | None:
        with self._lock:
            if not self._commit_failure_order:
                return None
            receipt = self._commit_failures[self._commit_failure_order[-1]]
        return _snapshot_production_runtime_audit_commit_failure_receipt(receipt)

    @property
    def latest_admission_failure_receipt(
        self,
    ) -> ProductionRuntimeAuditAdmissionFailureReceiptV1 | None:
        with self._lock:
            if not self._admission_failure_order:
                return None
            receipt = self._admission_failures[self._admission_failure_order[-1]]
        return _snapshot_production_runtime_audit_admission_failure_receipt(receipt)

    @property
    def pending_action_logical_call_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                logical_call_id
                for logical_call_id, pending in self._pending.items()
                if pending.parsed_action is not None
            )

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


__all__ = [
    "ExternalProductionRuntimeAuditSinkV1",
    "MemoryProductionRuntimeAuditSinkV1",
    "PRODUCTION_ACTOR_PROVIDER_ATTEMPT_SCHEMA_VERSION",
    "PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION",
    "PRODUCTION_RUNTIME_AUDIT_ADMISSION_FAILURE_RECEIPT_SCHEMA_VERSION",
    "PRODUCTION_RUNTIME_AUDIT_COMMIT_FAILURE_RECEIPT_SCHEMA_VERSION",
    "PRODUCTION_RUNTIME_AUDIT_FAILURE_RECEIPT_SCHEMA_VERSION",
    "PRODUCTION_RUNTIME_AUDIT_FAILURE_SCHEMA_VERSION",
    "PRODUCTION_RUNTIME_AUDIT_PRE_PROVIDER_SCHEMA_VERSION",
    "PRODUCTION_RUNTIME_AUDIT_RECEIPT_SCHEMA_VERSION",
    "ProductionActorProviderAttemptStatusV1",
    "ProductionActorProviderAttemptV1",
    "ProductionRuntimeAuditAdmissionFailureReceiptV1",
    "ProductionRuntimeAuditAdmissionStageV1",
    "ProductionRuntimeAuditDetailV1",
    "ProductionRuntimeAuditCommitFailureReceiptV1",
    "ProductionRuntimeAuditError",
    "ProductionRuntimeAuditFailureReceiptV1",
    "ProductionRuntimeAuditPreProviderOutcomeV1",
    "ProductionRuntimeAuditPreProviderV1",
    "ProductionRuntimeAuditPreProviderStatusV1",
    "ProductionRuntimeAuditPublicationStatusV1",
    "ProductionRuntimeAuditReceiptV1",
    "ProductionRuntimeAuditSinkV1",
    "ProductionRuntimeAuditSinkAdmissionError",
    "ProductionRuntimeAuditTerminalKindV1",
    "ProductionRuntimeAuditTransactionV1",
    "ProductionRuntimeAuditV1",
    "production_actor_provider_attempt_projection",
    "production_runtime_audit_admission_failure_receipt_projection",
    "production_runtime_audit_admission_failure_receipt_sha256",
    "production_runtime_audit_detail_projection",
    "production_runtime_audit_detail_sha256",
    "production_runtime_audit_commit_failure_receipt_projection",
    "production_runtime_audit_commit_failure_receipt_sha256",
    "production_runtime_audit_failure_receipt_projection",
    "production_runtime_audit_failure_receipt_sha256",
    "production_runtime_audit_pre_provider_projection",
    "production_runtime_audit_pre_provider_sha256",
    "production_runtime_audit_receipt_projection",
    "production_runtime_audit_receipt_sha256",
]
