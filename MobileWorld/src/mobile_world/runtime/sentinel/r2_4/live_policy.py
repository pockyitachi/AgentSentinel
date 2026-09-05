"""Owner-authorized live promotion boundary for the R2.4 history policy.

This module does not read a secret, open a network connection, probe a GPU,
load a model, start a backend, or execute an actor action.  It binds one exact
owner-authorized R2.4/R2.5 run manifest to one exact R2.2 GPT-5.6 policy
descriptor.  The wrapped policy remains R2.2 ``SHADOW_ONLY``; an injected,
module-owned promotion callback must construct the additive R2.4 live output.

The callback is deliberately required while the R2.4 output contracts are
being generalized from ``CPU_FAKE_ACTIVE``.  Returning a CPU-fake-scoped
output from a live evaluation is rejected rather than silently mislabelled.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import InitVar, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from threading import Lock
from time import monotonic_ns, perf_counter_ns
from typing import Any, cast

from jsonschema import Draft202012Validator, RefResolver  # type: ignore[import-untyped]

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonValue,
    copy_json,
)
from mobile_world.offline.causal_replay.contracts import (
    canonical_sha256 as portable_canonical_sha256,
)
from mobile_world.runtime.audit.context import get_audit_context
from mobile_world.runtime.audit.lifecycle import TaskAuditBinding
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.r2_2.contracts import (
    EvidencePacketV1,
    PolicyExecutionControlV1,
    RuntimeAdmissionBundleV1,
    RuntimeExecutionScope,
    RuntimeSentinelPolicyOutputV1,
    evidence_packet_sha256,
    runtime_policy_output_sha256,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56_MAX_OUTPUT_TOKENS,
    GPT56_OUTPUT_SCHEMA_NAME,
    GPT56_POLICY_INSTRUCTIONS,
    GPT56_REASONING_EFFORT,
    GPT56_REQUESTED_MODEL,
    SUPPORTED_OPENAI_SDK_VERSION,
    GPT56PolicyError,
    GPT56SentinelPolicy,
    OpenAIResponsesTransportBindingV1,
    PolicyCallProvenanceV1,
    ProposalSchemaSnapshotV1,
    ResponsesEnvelopeV1,
    TransportDescriptorV1,
    build_owner_authorized_openai_responses_transport,
    openai_responses_transport_binding_projection,
    openai_responses_transport_binding_sha256,
    responses_envelope_hash_projection,
    transport_descriptor_sha256,
)
from mobile_world.runtime.sentinel.r2_2.metrics import R22PolicyMetrics
from mobile_world.runtime.sentinel.r2_2.runtime_overlay import (
    admission_receipt_projector,
    bind_policy_receipt,
    proposal_admission,
)
from mobile_world.runtime.sentinel.r2_2.sidecar import (
    MemoryR22PolicyReceiptSink,
    PolicyEvaluationStatus,
    R22PolicyReceiptV1,
    detach_r22_policy_receipt,
    r22_policy_receipt_dict,
)
from mobile_world.runtime.sentinel.r2_3.contracts import TaskInstructionV1
from mobile_world.runtime.sentinel.r2_3.session import RubricTaskSession
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    RuntimeVerticalExecutionScope,
    RuntimeVerticalPolicyOutputV1,
    canonical_json_bytes,
    canonical_sha256,
    snapshot_json_value,
    snapshot_vertical_output,
    vertical_output_sha256,
)
from mobile_world.runtime.sentinel.r2_4.evidence import (
    CollectorEvidenceFactoryV1,
    rubric_evidence_snapshot_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    CanonicalHistoryPolicyRequestV1,
    LiveAttemptAuthorityV1,
    LiveAttemptCostStatusV1,
    LiveAttemptDeadlineBindingV1,
    LiveAttemptPricingV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    MemoryLiveAttemptReceiptSinkV1,
    ProductionOpenAIAttemptRunnerV1,
    build_canonical_history_policy_request,
    history_policy_transport_schema_v1,
    live_attempt_authority_projection,
    live_attempt_authority_sha256,
    live_attempt_cost_usd_micros,
    live_attempt_deadline_binding_projection,
    live_attempt_pricing_projection,
    live_attempt_pricing_sha256,
    live_attempt_receipt_sha256,
    live_attempt_worst_case_cost_usd_micros,
    parse_live_attempt_authority_projection,
    parse_live_attempt_deadline_binding_projection,
    snapshot_canonical_history_policy_request,
    snapshot_live_attempt_authority,
    snapshot_live_attempt_deadline_binding,
    snapshot_live_attempt_pricing,
    snapshot_live_attempt_receipt,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SmokeModeV1,
    authority_manifest_projection,
    authority_manifest_sha256,
    parse_authority_manifest,
)
from mobile_world.runtime.sentinel.r2_4.orchestration import (
    R24CoordinatedCallRecordV1,
    R24RuntimeCoordinatorV1,
)
from mobile_world.runtime.sentinel.r2_4.policy import promote_r22_policy_output
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    CaseExecutionLeaseV1,
    ProductionPostPreflightFactoryV1,
    case_execution_lease_projection,
    case_execution_lease_sha256,
    openai_stage_projection,
    openai_stage_set_sha256,
    openai_stage_sha256,
)
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
    LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION,
    BoundCollectorCurrentImageV1,
    LiveOpenAIRubricBackendV1,
    LiveRubricAttemptConstraintBindingV1,
    LiveRubricAttemptRequestAnchorV1,
    LiveRubricCallReceiptV1,
    LiveRubricCallTrustAnchorV1,
    LiveRubricExecutionScopeV1,
    LiveRubricOperationV1,
    ProductionRubricProviderPortV1,
    R24RubricBackendExtensionDescriptorV1,
    bind_current_collector_image_projection,
    build_live_rubric_attempt_constraint_binding_v1,
    live_rubric_attempt_constraint_binding_projection,
    live_rubric_call_receipt_sha256,
    live_rubric_operation_prompt_sha256,
    live_rubric_prompt_bundle_sha256,
    parse_durable_attempt_pricing_projection_v1,
    parse_durable_case_execution_lease_projection_v1,
    parse_durable_live_attempt_receipt_projection_v1,
    parse_durable_openai_stage_projection_v1,
    parse_live_rubric_attempt_constraint_binding_projection_v1,
    snapshot_live_rubric_attempt_constraint_binding,
    snapshot_live_rubric_attempt_request_anchor,
    snapshot_live_rubric_call_receipt,
    snapshot_live_rubric_call_trust_anchor,
    snapshot_r24_rubric_backend_extension_descriptor,
    validate_live_rubric_attempt_receipt_authority_v1,
    validate_live_rubric_attempt_request_anchor_v1,
    validate_live_rubric_request_anchor_v1,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    R24_SMOKE_AUTHORITY_SCHEMA_VERSION,
    R24SmokeRunAuthorityManifestV1,
    parse_smoke_authority_manifest,
    smoke_authority_manifest_projection,
    smoke_authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import PilotArmV1, PilotHostV1

OWNER_AUTHORIZED_LIVE_POLICY_AUTHORITY_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-owner-authorized-live-policy-authority/v1"
)
OWNER_AUTHORIZED_LIVE_ACTIVE_SCOPE_VALUE = "OWNER_AUTHORIZED_LIVE_ACTIVE"
LIVE_HISTORY_POLICY_REQUEST_PROOF_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-history-policy-request-proof/v1"
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_UTC_SECOND = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_HISTORY_DATA_IMAGE = re.compile(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})")
_LIVE_PROMOTION_CHECKS = (
    "R24_R22_EXACT_OUTPUT_BOUND",
    "R24_R22_RECEIPT_HASH_RETAINED",
    "R24_OWNER_AUTHORITY_MANIFEST_BOUND",
    "R24_LIVE_TRANSPORT_DESCRIPTOR_BOUND",
    "R24_LIVE_TRANSPORT_BINDING_BOUND",
    "R24_TARGET_CENSUS_BOUND",
    "R24_FIXED_REPLACEMENT_TEMPLATE_ONLY",
    "R24_FIXED_REPLACEMENT_STRONG_EVIDENCE_BOUND",
    "R24_NO_ACTION_OR_TOOL_AUTHORITY",
    "R24_ZERO_TARGET_NOOP_VALID",
)
_PER_CALL_POLICY_SEAL = object()
_LIVE_BUDGET_LEDGER_SEAL = object()
_HISTORY_REQUEST_PROOF_SEAL = object()
_R22_RECEIPT_PUBLICATION_FAILURE_CODES = frozenset(
    {
        "POLICY_RECEIPT_PREPARE_FAILED",
        "POLICY_RECEIPT_PREPARE_MUTATED",
        "POLICY_RECEIPT_PUBLISH_FAILED",
        "POLICY_RECEIPT_PUBLISH_MUTATED",
        "POLICY_DEADLINE_EXCEEDED",
    }
)


class LiveHistoryPolicyReceiptStateV1(StrEnum):
    R22_RECEIPT_COMMITTED = "R22_RECEIPT_COMMITTED"
    R22_RECEIPT_ABSENT_PRE_DISPATCH = "R22_RECEIPT_ABSENT_PRE_DISPATCH"
    R22_RECEIPT_ABSENT_POST_DISPATCH = "R22_RECEIPT_ABSENT_POST_DISPATCH"


@lru_cache(maxsize=1)
def _history_request_proof_validator() -> Draft202012Validator:
    schema_root = Path(__file__).resolve().parents[6] / "mobileworld_audit_handoff" / "schemas"
    paths = {
        "history": schema_root / "r2_4/history_policy_request_proof.v1.schema.json",
        "rubric": schema_root / "r2_4/rubric_request_proof.v1.schema.json",
        "receipt": schema_root / "r2_2/policy_receipt.v1.schema.json",
        "proposal": schema_root / "r2_2/policy_proposal.v1.schema.json",
        "evidence": schema_root / "r2_2/evidence_packet.v1.schema.json",
    }
    schemas: dict[str, dict[str, JsonValue]] = {}
    for name, path in paths.items():
        try:
            decoded = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise R24ContractError(
                "INVALID_HISTORY_REQUEST_PROOF_SCHEMA",
                "checked-in history request proof schema is unavailable",
            ) from exc
        if type(decoded) is not dict or type(decoded.get("$id")) is not str:
            raise R24ContractError(
                "INVALID_HISTORY_REQUEST_PROOF_SCHEMA",
                "checked-in history request proof schema set differs",
            )
        schemas[name] = cast(dict[str, JsonValue], decoded)
    primary = schemas["history"]
    try:
        Draft202012Validator.check_schema(primary)
    except Exception as exc:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF_SCHEMA",
            "checked-in history request proof schema is invalid",
        ) from exc
    resolver = RefResolver.from_schema(
        primary,
        store={cast(str, value["$id"]): value for value in schemas.values()},
    )
    return Draft202012Validator(primary, resolver=resolver)


@lru_cache(maxsize=1)
def _history_evidence_packet_validator() -> Draft202012Validator:
    path = (
        Path(__file__).resolve().parents[6]
        / "mobileworld_audit_handoff"
        / "schemas"
        / "r2_2"
        / "evidence_packet.v1.schema.json"
    )
    try:
        decoded = json.loads(path.read_bytes())
        if type(decoded) is not dict:
            raise TypeError("evidence packet schema root differs")
        Draft202012Validator.check_schema(decoded)
    except Exception as exc:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF_SCHEMA",
            "checked-in evidence packet schema is unavailable or invalid",
        ) from exc
    return Draft202012Validator(cast(dict[str, JsonValue], decoded))


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise R24ContractError("INVALID_SHA256", f"{name} must be lowercase SHA-256")
    return value


def _require_id(value: object, name: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise R24ContractError("INVALID_RUNTIME_ID", f"{name} is invalid")
    return value


def _trusted_now(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if type(current) is not datetime or current.tzinfo is None:
        raise R24ContractError("INVALID_AUTHORITY_TIME", "authority time must be timezone-aware")
    try:
        offset = current.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise R24ContractError("INVALID_AUTHORITY_TIME", "authority time is invalid") from exc
    if offset != timedelta(0):
        raise R24ContractError("INVALID_AUTHORITY_TIME", "authority time must be UTC")
    return current.astimezone(UTC)


def _parse_utc_second(value: str, name: str) -> datetime:
    if type(value) is not str or _UTC_SECOND.fullmatch(value) is None:
        raise R24ContractError("INVALID_AUTHORITY_TIME", f"{name} is not UTC to seconds")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise R24ContractError("INVALID_AUTHORITY_TIME", f"{name} is not a real time") from exc


def _history_policy_stage_projection(value: OpenAIResponsesStageV1) -> dict[str, JsonValue]:
    if type(value) is not OpenAIResponsesStageV1:
        raise R24ContractError(
            "UNTRUSTED_LIVE_AUTHORITY", "history policy stage has an untrusted type"
        )
    trusted = OpenAIResponsesStageV1(
        role=value.role,
        model=value.model,
        endpoint=value.endpoint,
        transport_kind=value.transport_kind,
        transport_authority=value.transport_authority,
        openai_sdk_version=value.openai_sdk_version,
        sdk_max_retries=value.sdk_max_retries,
        external_network_on_call=value.external_network_on_call,
        model_on_call=value.model_on_call,
        max_output_tokens=value.max_output_tokens,
        timeout_ms=value.timeout_ms,
        max_attempts=value.max_attempts,
        store=value.store,
    )
    if trusted.role is not OpenAIRoleV1.HISTORY_POLICY:
        raise R24ContractError(
            "LIVE_HISTORY_POLICY_STAGE_REQUIRED", "manifest stage is not HISTORY_POLICY"
        )
    return {
        "endpoint": trusted.endpoint,
        "external_network_on_call": trusted.external_network_on_call,
        "max_attempts": trusted.max_attempts,
        "max_output_tokens": trusted.max_output_tokens,
        "model": trusted.model,
        "model_on_call": trusted.model_on_call,
        "openai_sdk_version": trusted.openai_sdk_version,
        "role": trusted.role.value,
        "sdk_max_retries": trusted.sdk_max_retries,
        "store": trusted.store,
        "timeout_ms": trusted.timeout_ms,
        "transport_authority": trusted.transport_authority,
        "transport_kind": trusted.transport_kind,
    }


def history_policy_stage_sha256(value: OpenAIResponsesStageV1) -> str:
    return canonical_sha256(cast(JsonValue, _history_policy_stage_projection(value)))


def _live_manifest_sha256(
    value: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
) -> str:
    if isinstance(value, R24R25RunAuthorityManifestV1):
        return authority_manifest_sha256(value)
    if isinstance(value, R24SmokeRunAuthorityManifestV1):
        return smoke_authority_manifest_sha256(value)
    raise R24ContractError("UNTRUSTED_LIVE_AUTHORITY", "manifest type differs")


def _detach_manifest(
    value: object,
) -> tuple[R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1, bytes]:
    if type(value) is R24R25RunAuthorityManifestV1:
        projection = authority_manifest_projection(value)
        parser_kind = "JOINT"
    elif type(value) is R24SmokeRunAuthorityManifestV1:
        projection = smoke_authority_manifest_projection(value)
        parser_kind = "SMOKE"
    else:
        raise R24ContractError(
            "UNTRUSTED_LIVE_AUTHORITY", "manifest must use the exact authority type"
        )
    try:
        raw = canonical_json_bytes(cast(JsonValue, projection))
        decoded = json.loads(raw)
        detached = (
            parse_authority_manifest(decoded)
            if parser_kind == "JOINT"
            else parse_smoke_authority_manifest(decoded)
        )
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise R24ContractError(
            "UNTRUSTED_LIVE_AUTHORITY", "manifest could not be rebuilt as trusted data"
        ) from exc
    return detached, raw


def _manifest_from_canonical_bytes(
    value: object,
) -> R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1:
    if type(value) is not bytes or not value:
        raise R24ContractError(
            "UNTRUSTED_LIVE_AUTHORITY", "authority does not retain canonical manifest bytes"
        )
    raw = value
    try:
        decoded = json.loads(raw)
        if type(decoded) is not dict:
            raise ValueError("manifest must be an object")
        manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1
        if decoded.get("schema_version") == R24_SMOKE_AUTHORITY_SCHEMA_VERSION:
            smoke_manifest = parse_smoke_authority_manifest(decoded)
            manifest = smoke_manifest
            projection = smoke_authority_manifest_projection(smoke_manifest)
        else:
            joint_manifest = parse_authority_manifest(decoded)
            manifest = joint_manifest
            projection = authority_manifest_projection(joint_manifest)
        rebuilt = canonical_json_bytes(cast(JsonValue, projection))
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise R24ContractError(
            "UNTRUSTED_LIVE_AUTHORITY", "bound manifest is not trusted canonical data"
        ) from exc
    if rebuilt != raw:
        raise R24ContractError(
            "MANIFEST_CANONICAL_BINDING_MISMATCH", "bound manifest bytes are not canonical"
        )
    return manifest


def _history_policy_stage(
    manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
) -> OpenAIResponsesStageV1:
    stages = tuple(
        stage for stage in manifest.openai_stages if stage.role is OpenAIRoleV1.HISTORY_POLICY
    )
    if len(stages) != 1 or type(stages[0]) is not OpenAIResponsesStageV1:
        raise R24ContractError(
            "LIVE_HISTORY_POLICY_STAGE_REQUIRED",
            "manifest must contain one exact HISTORY_POLICY stage",
        )
    return stages[0]


def _require_manifest_authority_current(
    manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
    now: datetime,
) -> OpenAIResponsesStageV1:
    confirmed = _require_sha256(confirmed_manifest_sha256, "confirmed_manifest_sha256")
    try:
        actual = _live_manifest_sha256(manifest)
    except (TypeError, ValueError, RecursionError) as exc:
        raise R24ContractError(
            "UNTRUSTED_LIVE_AUTHORITY", "manifest hash could not be recomputed"
        ) from exc
    if actual != confirmed:
        raise R24ContractError("MANIFEST_CONFIRMATION_MISMATCH", "confirmed manifest hash differs")
    authorization = manifest.authorization
    if authorization.status is not RunAuthorizationStatusV1.OWNER_AUTHORIZED:
        raise R24ContractError(
            "OWNER_AUTHORITY_REQUIRED", "manifest is not explicitly owner-authorized"
        )
    issued = _parse_utc_second(authorization.issued_at_utc, "issued_at_utc")
    expires = _parse_utc_second(authorization.expires_at_utc, "expires_at_utc")
    if not issued <= now < expires:
        raise R24ContractError("OWNER_AUTHORITY_EXPIRED", "owner authorization is not current")
    if not (
        authorization.network_allowed is True
        and authorization.sentinel_provider_calls_allowed is True
        and authorization.model_loading_allowed is True
    ):
        raise R24ContractError(
            "LIVE_POLICY_AUTHORITY_REQUIRED",
            "manifest does not authorize every live policy resource",
        )
    return _history_policy_stage(manifest)


@dataclass(frozen=True, slots=True)
class OwnerAuthorizedLivePolicyAuthorityV1:
    """Detached manifest/time binding used by the live policy adapter.

    The canonical bytes contain manifest metadata only.  The manifest contract
    carries a path to a secret file, never the secret value or a value hash.
    """

    manifest_sha256: str
    history_policy_stage_sha256: str
    authorization_id: str
    run_id: str
    source_commit: str
    authorized_at_utc: str
    _manifest_canonical_bytes: bytes = field(repr=False)
    schema_version: str = OWNER_AUTHORIZED_LIVE_POLICY_AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OWNER_AUTHORIZED_LIVE_POLICY_AUTHORITY_SCHEMA_VERSION:
            raise R24ContractError("UNKNOWN_SCHEMA_VERSION", "unknown live authority schema")
        _require_sha256(self.manifest_sha256, "manifest_sha256")
        _require_sha256(self.history_policy_stage_sha256, "history_policy_stage_sha256")
        _require_id(self.authorization_id, "authorization_id")
        _require_id(self.run_id, "run_id")
        if type(self.source_commit) is not str or _GIT_SHA1.fullmatch(self.source_commit) is None:
            raise R24ContractError("INVALID_COMMIT", "source_commit must be full SHA-1")
        bound_at = _parse_utc_second(self.authorized_at_utc, "authorized_at_utc")
        manifest = _manifest_from_canonical_bytes(self._manifest_canonical_bytes)
        stage = _require_manifest_authority_current(
            manifest,
            confirmed_manifest_sha256=self.manifest_sha256,
            now=bound_at,
        )
        if (
            manifest.authorization.authorization_id != self.authorization_id
            or manifest.run_id != self.run_id
            or manifest.source_commit != self.source_commit
            or history_policy_stage_sha256(stage) != self.history_policy_stage_sha256
        ):
            raise R24ContractError(
                "LIVE_AUTHORITY_BINDING_MISMATCH",
                "authority fields differ from the confirmed manifest",
            )

    def manifest_snapshot(
        self,
    ) -> R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1:
        """Return a fresh exact manifest graph without touching referenced files."""

        return _manifest_from_canonical_bytes(self._manifest_canonical_bytes)


def owner_authorized_live_policy_authority_projection(
    value: OwnerAuthorizedLivePolicyAuthorityV1,
) -> dict[str, JsonValue]:
    if type(value) is not OwnerAuthorizedLivePolicyAuthorityV1:
        raise R24ContractError("UNTRUSTED_LIVE_AUTHORITY", "authority must use the exact live type")
    manifest = value.manifest_snapshot()
    stage = _require_manifest_authority_current(
        manifest,
        confirmed_manifest_sha256=value.manifest_sha256,
        now=_parse_utc_second(value.authorized_at_utc, "authorized_at_utc"),
    )
    if history_policy_stage_sha256(stage) != value.history_policy_stage_sha256:
        raise R24ContractError(
            "LIVE_AUTHORITY_BINDING_MISMATCH", "history policy stage binding differs"
        )
    return {
        "authorization_id": value.authorization_id,
        "authorized_at_utc": value.authorized_at_utc,
        "history_policy_stage_sha256": value.history_policy_stage_sha256,
        "manifest_sha256": value.manifest_sha256,
        "run_id": value.run_id,
        "schema_version": value.schema_version,
        "source_commit": value.source_commit,
    }


def owner_authorized_live_policy_authority_sha256(
    value: OwnerAuthorizedLivePolicyAuthorityV1,
) -> str:
    return canonical_sha256(
        cast(JsonValue, owner_authorized_live_policy_authority_projection(value))
    )


def issue_owner_authorized_live_policy_authority(
    manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
    *,
    confirmed_manifest_sha256: str,
    now: datetime | None = None,
) -> OwnerAuthorizedLivePolicyAuthorityV1:
    """Bind a current owner-approved manifest without reading any resource path."""

    trusted, raw = _detach_manifest(manifest)
    current = _trusted_now(now)
    stage = _require_manifest_authority_current(
        trusted,
        confirmed_manifest_sha256=confirmed_manifest_sha256,
        now=current,
    )
    authority = OwnerAuthorizedLivePolicyAuthorityV1(
        manifest_sha256=_live_manifest_sha256(trusted),
        history_policy_stage_sha256=history_policy_stage_sha256(stage),
        authorization_id=trusted.authorization.authorization_id,
        run_id=trusted.run_id,
        source_commit=trusted.source_commit,
        authorized_at_utc=current.strftime("%Y-%m-%dT%H:%M:%SZ"),
        _manifest_canonical_bytes=raw,
    )
    # Return a second exact instance so no object reachable from the caller's
    # manifest is retained by the issued authority.
    return OwnerAuthorizedLivePolicyAuthorityV1(
        manifest_sha256=authority.manifest_sha256,
        history_policy_stage_sha256=authority.history_policy_stage_sha256,
        authorization_id=authority.authorization_id,
        run_id=authority.run_id,
        source_commit=authority.source_commit,
        authorized_at_utc=authority.authorized_at_utc,
        _manifest_canonical_bytes=bytes(authority._manifest_canonical_bytes),
    )


def promote_owner_authorized_live_policy_output(
    source: RuntimeSentinelPolicyOutputV1,
    *,
    policy_id: str,
    authority_manifest_sha256: str,
    source_transport_descriptor_sha256: str,
    source_transport_binding_sha256: str,
    replace_drop_target_ids: tuple[str, ...] = (),
) -> RuntimeVerticalPolicyOutputV1:
    """Construct live-scoped data; the exact adapter remains the authority gate."""

    _require_sha256(authority_manifest_sha256, "authority_manifest_sha256")
    _require_sha256(source_transport_binding_sha256, "source_transport_binding_sha256")
    return promote_r22_policy_output(
        source,
        policy_id=policy_id,
        source_transport_descriptor_sha256=source_transport_descriptor_sha256,
        source_transport_binding_sha256=source_transport_binding_sha256,
        replace_drop_target_ids=replace_drop_target_ids,
        authority=None,
        execution_scope=RuntimeVerticalExecutionScope.OWNER_AUTHORIZED_LIVE_ACTIVE,
        execution_authority_sha256=authority_manifest_sha256,
        validation_checks=_LIVE_PROMOTION_CHECKS,
    )


def _validate_replace_targets(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "replace targets must be a tuple")
    targets = cast(tuple[object, ...], value)
    if len(targets) > 256 or any(type(item) is not str or not item for item in targets):
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "replace target IDs are invalid")
    projected = cast(tuple[str, ...], targets)
    if len(projected) != len(set(projected)):
        raise R24ContractError("DUPLICATE_RUNTIME_ID", "replace target IDs repeat")
    return tuple(projected)


def _live_descriptor_from_exact_policy(
    source_policy: object,
) -> tuple[TransportDescriptorV1, str]:
    if type(source_policy) is not GPT56SentinelPolicy:
        raise R24ContractError(
            "LIVE_SOURCE_ATTESTATION_REQUIRED",
            "only the exact descriptor-bound R2.2 GPT policy may be promoted",
        )
    try:
        descriptor = source_policy.transport_descriptor
        reported_sha256 = source_policy.transport_descriptor_sha256
    except Exception as exc:
        raise R24ContractError(
            "LIVE_SOURCE_ATTESTATION_REQUIRED", "source descriptor is unavailable"
        ) from exc
    if type(descriptor) is not TransportDescriptorV1:
        raise R24ContractError(
            "LIVE_SOURCE_ATTESTATION_REQUIRED", "source descriptor has an untrusted type"
        )
    trusted = TransportDescriptorV1(
        transport_kind=descriptor.transport_kind,
        transport_authority=descriptor.transport_authority,
        openai_sdk_version=descriptor.openai_sdk_version,
        sdk_max_retries=descriptor.sdk_max_retries,
        external_network_on_call=descriptor.external_network_on_call,
        model_on_call=descriptor.model_on_call,
    )
    if (
        trusted.transport_kind != "OPENAI_RESPONSES"
        or trusted.transport_authority != "EXPLICIT_OWNER_AUTHORIZATION"
        or trusted.openai_sdk_version != SUPPORTED_OPENAI_SDK_VERSION
        or trusted.sdk_max_retries != 0
        or trusted.external_network_on_call is not True
        or trusted.model_on_call is not True
    ):
        raise R24ContractError(
            "LIVE_SOURCE_ATTESTATION_REQUIRED", "source is not the exact live Responses policy"
        )
    digest = transport_descriptor_sha256(trusted)
    if type(reported_sha256) is not str or reported_sha256 != digest:
        raise R24ContractError(
            "SOURCE_AUTHORITY_DRIFT", "source descriptor and descriptor hash differ"
        )
    return trusted, digest


def _require_descriptor_matches_stage(
    descriptor: TransportDescriptorV1,
    stage: OpenAIResponsesStageV1,
) -> None:
    if (
        descriptor.transport_kind != stage.transport_kind
        or descriptor.transport_authority != stage.transport_authority
        or descriptor.openai_sdk_version != stage.openai_sdk_version
        or descriptor.sdk_max_retries != stage.sdk_max_retries
        or descriptor.external_network_on_call is not stage.external_network_on_call
        or descriptor.model_on_call is not stage.model_on_call
    ):
        raise R24ContractError(
            "LIVE_DESCRIPTOR_MANIFEST_MISMATCH",
            "source descriptor differs from the confirmed history-policy stage",
        )


def _live_transport_binding_from_exact_policy(
    source_policy: GPT56SentinelPolicy[object, object],
) -> tuple[OpenAIResponsesTransportBindingV1, str]:
    try:
        binding = source_policy.assert_live_transport_binding()
    except Exception as exc:
        raise R24ContractError(
            "LIVE_TRANSPORT_ATTESTATION_REQUIRED",
            "source policy lacks its exact construction-bound live transport",
        ) from exc
    if type(binding) is not OpenAIResponsesTransportBindingV1:
        raise R24ContractError(
            "LIVE_TRANSPORT_ATTESTATION_REQUIRED", "live transport binding is untrusted"
        )
    try:
        digest = openai_responses_transport_binding_sha256(binding)
    except (TypeError, ValueError, RecursionError) as exc:
        raise R24ContractError(
            "LIVE_TRANSPORT_ATTESTATION_REQUIRED", "live transport binding is invalid"
        ) from exc
    return binding, digest


def _require_transport_binding_matches_stage(
    binding: OpenAIResponsesTransportBindingV1,
    *,
    descriptor_sha256: str,
    authority_manifest_sha256: str,
    stage: OpenAIResponsesStageV1,
) -> None:
    stage_timeout_ns = stage.timeout_ms * 1_000_000
    if (
        binding.descriptor_sha256 != descriptor_sha256
        or binding.responses_endpoint != stage.endpoint
        or binding.requested_model != stage.model
        or binding.max_output_tokens != stage.max_output_tokens
        or binding.seam_policy_deadline_ns != stage_timeout_ns
        or binding.transport_timeout_ns > stage_timeout_ns
        or binding.client_timeout_ceiling_ns > stage_timeout_ns
    ):
        raise R24ContractError(
            "LIVE_TRANSPORT_MANIFEST_MISMATCH",
            "exact live transport binding differs from the confirmed manifest stage",
        )
    if (
        binding.client_origin != "MODULE_OWNED_PRODUCTION"
        or binding.environment_proxy_disabled is not True
    ):
        raise R24ContractError(
            "LIVE_PRODUCTION_TRANSPORT_REQUIRED",
            "caller-injected or ambient-proxy transport cannot receive live authority",
        )
    if binding.authority_manifest_sha256 != authority_manifest_sha256:
        raise R24ContractError(
            "LIVE_TRANSPORT_MANIFEST_MISMATCH",
            "production transport was sealed for another owner manifest",
        )


class _DirectLivePolicyExecutionControl:
    def __init__(self, timeout_ms: int) -> None:
        self._deadline_ns = perf_counter_ns() + timeout_ms * 1_000_000
        self._transport_authorized = False
        self._receipt_published = False
        self._lock = Lock()

    def _require_current(self) -> None:
        if perf_counter_ns() >= self._deadline_ns:
            raise TimeoutError("live history-policy execution deadline elapsed")

    def run_transport[T](self, call: Callable[[], T]) -> T:
        if not callable(call):
            raise TypeError("transport callback must be callable")
        with self._lock:
            self._require_current()
            if self._transport_authorized:
                raise RuntimeError("live history-policy transport was already authorized")
            self._transport_authorized = True
            return call()

    def publish_receipt(self, publish: Callable[[], None]) -> None:
        if not callable(publish):
            raise TypeError("receipt callback must be callable")
        with self._lock:
            self._require_current()
            if self._receipt_published:
                raise RuntimeError("live history-policy receipt was already published")
            publish()
            self._receipt_published = True


class R22OwnerAuthorizedLivePolicyAdapter:
    """Promote one exact, current, owner-authorized live R2.2 evaluation."""

    def __init__(
        self,
        source_policy: object,
        *,
        authority: OwnerAuthorizedLivePolicyAuthorityV1,
        replace_drop_target_ids: tuple[str, ...] = (),
        _per_call_policy_id: str | None = None,
        _per_call_seal: object | None = None,
    ) -> None:
        if (_per_call_policy_id is None) != (_per_call_seal is None):
            raise PermissionError("per-call policy override is incomplete")
        if _per_call_policy_id is not None:
            if _per_call_seal is not _PER_CALL_POLICY_SEAL:
                raise PermissionError("per-call policy override is module-owned")
            _require_id(_per_call_policy_id, "per_call_policy_id")
        if type(authority) is not OwnerAuthorizedLivePolicyAuthorityV1:
            raise R24ContractError(
                "LIVE_POLICY_AUTHORITY_REQUIRED", "adapter needs the exact live authority"
            )
        manifest = authority.manifest_snapshot()
        stage = _require_manifest_authority_current(
            manifest,
            confirmed_manifest_sha256=authority.manifest_sha256,
            now=_trusted_now(None),
        )
        descriptor, descriptor_sha256 = _live_descriptor_from_exact_policy(source_policy)
        _require_descriptor_matches_stage(descriptor, stage)
        trusted_source = cast(GPT56SentinelPolicy[object, object], source_policy)
        try:
            source_scope = trusted_source.execution_scope
            source_policy_id = trusted_source.policy_id
        except Exception as exc:
            raise R24ContractError(
                "UNTRUSTED_R22_POLICY", "source policy metadata could not be read"
            ) from exc
        if (
            type(source_scope) is not RuntimeExecutionScope
            or source_scope is not RuntimeExecutionScope.SHADOW_ONLY
        ):
            raise R24ContractError(
                "R22_SHADOW_SOURCE_REQUIRED", "live adapter source must remain R2.2 SHADOW_ONLY"
            )
        if type(source_policy_id) is not str or not source_policy_id:
            raise R24ContractError("UNTRUSTED_R22_POLICY", "source policy ID is invalid")
        transport_binding, transport_binding_sha256 = _live_transport_binding_from_exact_policy(
            trusted_source
        )
        _require_transport_binding_matches_stage(
            transport_binding,
            descriptor_sha256=descriptor_sha256,
            authority_manifest_sha256=authority.manifest_sha256,
            stage=stage,
        )
        self._source_policy = trusted_source
        self._source_policy_id = source_policy_id
        self._source_descriptor_sha256 = descriptor_sha256
        self._source_transport_binding_sha256 = transport_binding_sha256
        self._replace_drop_target_ids = _validate_replace_targets(replace_drop_target_ids)
        self._authority = OwnerAuthorizedLivePolicyAuthorityV1(
            manifest_sha256=authority.manifest_sha256,
            history_policy_stage_sha256=authority.history_policy_stage_sha256,
            authorization_id=authority.authorization_id,
            run_id=authority.run_id,
            source_commit=authority.source_commit,
            authorized_at_utc=authority.authorized_at_utc,
            _manifest_canonical_bytes=bytes(authority._manifest_canonical_bytes),
        )
        policy_subject = (
            f"{source_policy_id}\x00{authority.manifest_sha256}\x00{descriptor_sha256}"
            f"\x00{transport_binding_sha256}"
        ).encode()
        derived_policy_id = f"r24-owner-live-{hashlib.sha256(policy_subject).hexdigest()[:32]}"
        self._policy_id = derived_policy_id if _per_call_policy_id is None else _per_call_policy_id

    @property
    def policy_id(self) -> str:
        return self._policy_id

    @property
    def source_policy_id(self) -> str:
        return self._source_policy_id

    @property
    def authority_manifest_sha256(self) -> str:
        return self._authority.manifest_sha256

    @property
    def authority_sha256(self) -> str:
        return owner_authorized_live_policy_authority_sha256(self._authority)

    @property
    def execution_authority_sha256(self) -> str:
        """Bind live execution to the exact owner-confirmed manifest."""

        return self._authority.manifest_sha256

    @property
    def source_transport_descriptor_sha256(self) -> str:
        return self._source_descriptor_sha256

    @property
    def source_transport_binding_sha256(self) -> str:
        return self._source_transport_binding_sha256

    @property
    def execution_scope(self) -> RuntimeVerticalExecutionScope:
        return RuntimeVerticalExecutionScope.OWNER_AUTHORIZED_LIVE_ACTIVE

    @staticmethod
    def _detached_inputs(
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> tuple[JsonValue, SentinelContext, HistoryIR]:
        if type(context) is not SentinelContext or type(history_ir) is not HistoryIR:
            raise R24ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "live adapter inputs require exact trusted contracts"
            )
        request_copy = snapshot_json_value(request)
        try:
            context_copy = deepcopy(context)
            history_copy = deepcopy(history_ir)
        except (TypeError, ValueError, RecursionError) as exc:
            raise R24ContractError("GRAPH_SNAPSHOT_FAILED", "live input detach failed") from exc
        if type(context_copy) is not SentinelContext or type(history_copy) is not HistoryIR:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "live detach changed input types")
        return request_copy, context_copy, history_copy

    def _require_bindings_current(self) -> OpenAIResponsesStageV1:
        manifest = self._authority.manifest_snapshot()
        stage = _require_manifest_authority_current(
            manifest,
            confirmed_manifest_sha256=self._authority.manifest_sha256,
            now=_trusted_now(None),
        )
        if history_policy_stage_sha256(stage) != self._authority.history_policy_stage_sha256:
            raise R24ContractError(
                "LIVE_AUTHORITY_BINDING_MISMATCH", "history policy stage binding changed"
            )
        descriptor, digest = _live_descriptor_from_exact_policy(self._source_policy)
        if digest != self._source_descriptor_sha256:
            raise R24ContractError(
                "SOURCE_AUTHORITY_DRIFT", "source descriptor changed after adapter construction"
            )
        _require_descriptor_matches_stage(descriptor, stage)
        binding, binding_sha256 = _live_transport_binding_from_exact_policy(self._source_policy)
        if binding_sha256 != self._source_transport_binding_sha256:
            raise R24ContractError(
                "LIVE_TRANSPORT_BINDING_DRIFT",
                "live transport binding changed after adapter construction",
            )
        _require_transport_binding_matches_stage(
            binding,
            descriptor_sha256=digest,
            authority_manifest_sha256=self._authority.manifest_sha256,
            stage=stage,
        )
        return stage

    def _promote(self, source: RuntimeSentinelPolicyOutputV1) -> RuntimeVerticalPolicyOutputV1:
        if type(source) is not RuntimeSentinelPolicyOutputV1:
            raise R24ContractError("UNTRUSTED_R22_OUTPUT", "source output type is untrusted")
        output = promote_owner_authorized_live_policy_output(
            source,
            policy_id=self._policy_id,
            authority_manifest_sha256=self.execution_authority_sha256,
            source_transport_descriptor_sha256=self._source_descriptor_sha256,
            source_transport_binding_sha256=self._source_transport_binding_sha256,
            replace_drop_target_ids=self._replace_drop_target_ids,
        )
        if type(output) is not RuntimeVerticalPolicyOutputV1:
            raise R24ContractError(
                "LIVE_PROMOTION_REJECTED", "promotion returned an untrusted output type"
            )
        expected_scope = self.execution_scope
        if output.execution_scope is not expected_scope:
            raise R24ContractError(
                "LIVE_PROMOTION_SCOPE_MISMATCH", "promotion did not retain live scope"
            )
        if (
            output.policy_id != self._policy_id
            or output.source_policy_output_sha256 != runtime_policy_output_sha256(source)
            or output.source_policy_receipt_sha256 != source.policy_receipt_sha256
            or output.source_transport_descriptor_sha256 != self._source_descriptor_sha256
            or output.source_transport_binding_sha256 != self._source_transport_binding_sha256
            or output.admitted_plan.source_transport_binding_sha256
            != self._source_transport_binding_sha256
            or output.execution_authority_sha256 != self.execution_authority_sha256
            or output.admitted_plan.execution_authority_sha256 != self.execution_authority_sha256
        ):
            raise R24ContractError(
                "LIVE_PROMOTION_BINDING_MISMATCH", "promoted output differs from live source"
            )
        return output

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> RuntimeVerticalPolicyOutputV1:
        stage = self._require_bindings_current()
        control = _DirectLivePolicyExecutionControl(stage.timeout_ms)
        return self.evaluate_with_control(
            request=request,
            context=context,
            history_ir=history_ir,
            execution_control=control,
        )

    def evaluate_with_control(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        execution_control: PolicyExecutionControlV1,
    ) -> RuntimeVerticalPolicyOutputV1:
        self._require_bindings_current()
        if not isinstance(execution_control, PolicyExecutionControlV1):
            raise R24ContractError(
                "UNTRUSTED_EXECUTION_CONTROL", "live evaluation control is untrusted"
            )
        request_copy, context_copy, history_copy = self._detached_inputs(
            request, context, history_ir
        )
        source = self._source_policy.evaluate_with_control(
            request=request_copy,
            context=context_copy,
            history_ir=history_copy,
            execution_control=execution_control,
        )
        if type(source) is not RuntimeSentinelPolicyOutputV1:
            raise R24ContractError("UNTRUSTED_R22_OUTPUT", "source output type is untrusted")
        return self._promote(source)


@dataclass(frozen=True, slots=True)
class OwnerAuthorizedLiveCaseDescriptorV1:
    """One exact manifest case; it contains authority metadata, never a secret."""

    stage: RunStageV1
    host: PilotHostV1
    mode: SmokeModeV1
    case_id: str
    task_id: str
    task_parameters_sha256: str | None
    reset_seed: int | None
    max_actor_calls: int
    max_openai_calls: int
    max_wall_time_seconds: int
    max_cost_usd_micros: int

    def __post_init__(self) -> None:
        if type(self.stage) is not RunStageV1 or self.stage is RunStageV1.RESOURCE_PREFLIGHT:
            raise R24ContractError("INVALID_CASE_DESCRIPTOR", "case stage is not executable")
        if type(self.host) is not PilotHostV1 or type(self.mode) is not SmokeModeV1:
            raise R24ContractError("INVALID_CASE_DESCRIPTOR", "case host or mode is untrusted")
        _require_id(self.case_id, "case_id")
        _require_id(self.task_id, "task_id")
        if self.task_parameters_sha256 is not None:
            _require_sha256(self.task_parameters_sha256, "task_parameters_sha256")
        if self.reset_seed is not None and (
            type(self.reset_seed) is not int or not 0 <= self.reset_seed <= 2_147_483_647
        ):
            raise R24ContractError("INVALID_CASE_DESCRIPTOR", "reset_seed is invalid")
        if (self.task_parameters_sha256 is None) != (self.reset_seed is None):
            raise R24ContractError(
                "INVALID_CASE_DESCRIPTOR", "pilot task authority is partially bound"
            )
        for value, name in (
            (self.max_actor_calls, "max_actor_calls"),
            (self.max_openai_calls, "max_openai_calls"),
            (self.max_wall_time_seconds, "max_wall_time_seconds"),
            (self.max_cost_usd_micros, "max_cost_usd_micros"),
        ):
            if type(value) is not int or value <= 0:
                raise R24ContractError("INVALID_CASE_DESCRIPTOR", f"{name} must be positive")
        if self.mode is SmokeModeV1.OFF:
            raise R24ContractError(
                "LIVE_POLICY_FOR_OFF_FORBIDDEN", "OFF cases cannot construct a live resolver"
            )


@dataclass(frozen=True, slots=True)
class ResolvedLivePolicyCallBindingV1:
    """Immutable proof selected for one exact actor request and logical call."""

    logical_call_id: str
    actor_call_index: int
    actor_request_sha256: str
    policy_id: str
    execution_authority_sha256: str
    source_transport_descriptor_sha256: str
    source_transport_binding_sha256: str | None
    case_execution_lease_sha256: str
    preflight_report_sha256: str
    factory_binding_sha256: str
    pricing_binding_sha256: str
    rubric_backend_extension_descriptor_sha256: str
    rubric_attempt_receipt_sha256s: tuple[str, ...]
    rubric_call_receipt_sha256s: tuple[str, ...]
    history_policy_attempt_receipt_sha256: str | None
    output_sha256: str | None
    openai_calls: int
    cost_usd_micros: int

    def __post_init__(self) -> None:
        _require_id(self.logical_call_id, "logical_call_id")
        _require_id(self.policy_id, "policy_id")
        if type(self.actor_call_index) is not int or self.actor_call_index <= 0:
            raise R24ContractError("INVALID_CALL_BINDING", "actor call index is invalid")
        for name in (
            "actor_request_sha256",
            "execution_authority_sha256",
            "source_transport_descriptor_sha256",
            "case_execution_lease_sha256",
            "preflight_report_sha256",
            "factory_binding_sha256",
            "pricing_binding_sha256",
            "rubric_backend_extension_descriptor_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        for name in (
            "source_transport_binding_sha256",
            "history_policy_attempt_receipt_sha256",
            "output_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(value, name)
        if type(self.rubric_attempt_receipt_sha256s) is not tuple or not (
            1 <= len(self.rubric_attempt_receipt_sha256s) <= 2
        ):
            raise R24ContractError(
                "INVALID_CALL_BINDING", "one call needs rubric tracking and optional generation"
            )
        for value in self.rubric_attempt_receipt_sha256s:
            _require_sha256(value, "rubric_attempt_receipt_sha256")
        if type(self.rubric_call_receipt_sha256s) is not tuple or len(
            self.rubric_call_receipt_sha256s
        ) != len(self.rubric_attempt_receipt_sha256s):
            raise R24ContractError(
                "INVALID_CALL_BINDING",
                "R2.4 rubric-call and provider-attempt receipt census differs",
            )
        for value in self.rubric_call_receipt_sha256s:
            _require_sha256(value, "rubric_call_receipt_sha256")
        history_policy_present = self.history_policy_attempt_receipt_sha256 is not None
        if history_policy_present != (
            self.source_transport_binding_sha256 is not None
        ) or history_policy_present != (self.output_sha256 is not None):
            raise R24ContractError(
                "INVALID_CALL_BINDING",
                "history-policy receipt, transport binding, and output must be jointly present",
            )
        if self.openai_calls != len(self.rubric_attempt_receipt_sha256s) + int(
            history_policy_present
        ):
            raise R24ContractError("INVALID_CALL_BINDING", "OpenAI role census differs")
        if type(self.cost_usd_micros) is not int or self.cost_usd_micros < 0:
            raise R24ContractError("INVALID_CALL_BINDING", "call cost is invalid")


@dataclass(frozen=True, slots=True)
class _AmbientProductionCaseCallV1:
    policy_id: str
    factory_binding_sha256: str
    task_run_id: str
    task_recorder: object
    actor_call_index: int
    expected_actor_request_sha256: str | None


_AMBIENT_PRODUCTION_CASE_CALL: ContextVar[_AmbientProductionCaseCallV1 | None] = ContextVar(
    "mobileworld_r24_production_case_call",
    default=None,
)


def resolved_live_policy_call_binding_projection(
    value: ResolvedLivePolicyCallBindingV1,
) -> dict[str, JsonValue]:
    """Canonical public projection of one immutable production call binding."""

    if type(value) is not ResolvedLivePolicyCallBindingV1:
        raise R24ContractError("UNTRUSTED_CALL_BINDING", "call binding type differs")
    return {
        "logical_call_id": value.logical_call_id,
        "actor_call_index": value.actor_call_index,
        "actor_request_sha256": value.actor_request_sha256,
        "policy_id": value.policy_id,
        "execution_authority_sha256": value.execution_authority_sha256,
        "source_transport_descriptor_sha256": value.source_transport_descriptor_sha256,
        "source_transport_binding_sha256": value.source_transport_binding_sha256,
        "case_execution_lease_sha256": value.case_execution_lease_sha256,
        "preflight_report_sha256": value.preflight_report_sha256,
        "factory_binding_sha256": value.factory_binding_sha256,
        "pricing_binding_sha256": value.pricing_binding_sha256,
        "rubric_backend_extension_descriptor_sha256": (
            value.rubric_backend_extension_descriptor_sha256
        ),
        "rubric_attempt_receipt_sha256s": list(value.rubric_attempt_receipt_sha256s),
        "rubric_call_receipt_sha256s": list(value.rubric_call_receipt_sha256s),
        "history_policy_attempt_receipt_sha256": value.history_policy_attempt_receipt_sha256,
        "output_sha256": value.output_sha256,
        "openai_calls": value.openai_calls,
        "cost_usd_micros": value.cost_usd_micros,
    }


def resolved_live_policy_call_binding_sha256(
    value: ResolvedLivePolicyCallBindingV1,
) -> str:
    return canonical_sha256(cast(JsonValue, resolved_live_policy_call_binding_projection(value)))


def _snapshot_history_transport_binding(
    value: OpenAIResponsesTransportBindingV1,
) -> OpenAIResponsesTransportBindingV1:
    if type(value) is not OpenAIResponsesTransportBindingV1:
        raise R24ContractError(
            "UNTRUSTED_HISTORY_REQUEST_PROOF", "history transport binding type differs"
        )
    try:
        return OpenAIResponsesTransportBindingV1(
            **{name: getattr(value, name) for name in value.__dataclass_fields__}
        )
    except Exception as exc:
        raise R24ContractError(
            "UNTRUSTED_HISTORY_REQUEST_PROOF",
            "history transport binding could not be detached",
        ) from exc


def _snapshot_history_response_envelope(
    value: ResponsesEnvelopeV1,
) -> ResponsesEnvelopeV1:
    if type(value) is not ResponsesEnvelopeV1:
        raise R24ContractError(
            "UNTRUSTED_HISTORY_REQUEST_PROOF", "history response envelope type differs"
        )
    try:
        return ResponsesEnvelopeV1(
            response_id=value.response_id,
            requested_model=value.requested_model,
            returned_model=value.returned_model,
            status=value.status,
            service_tier=value.service_tier,
            output_text=value.output_text,
            input_tokens=value.input_tokens,
            output_tokens=value.output_tokens,
            total_tokens=value.total_tokens,
            schema_version=value.schema_version,
        )
    except Exception as exc:
        raise R24ContractError(
            "UNTRUSTED_HISTORY_REQUEST_PROOF",
            "history response envelope could not be detached",
        ) from exc


def _history_provider_request_projection(
    value: CanonicalHistoryPolicyRequestV1,
) -> dict[str, JsonValue]:
    trusted = snapshot_canonical_history_policy_request(value)
    try:
        projection = json.loads(trusted.canonical_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history provider request is invalid"
        ) from exc
    if (
        type(projection) is not dict
        or canonical_json_bytes(cast(JsonValue, projection)) != trusted.canonical_bytes
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history provider request is not canonical"
        )
    return cast(dict[str, JsonValue], projection)


def _history_response_envelope_projection(
    value: ResponsesEnvelopeV1,
) -> dict[str, JsonValue]:
    trusted = _snapshot_history_response_envelope(value)
    return {
        "input_tokens": trusted.input_tokens,
        "output_text": trusted.output_text,
        "output_tokens": trusted.output_tokens,
        "requested_model": trusted.requested_model,
        "response_id": trusted.response_id,
        "returned_model": trusted.returned_model,
        "schema_version": trusted.schema_version,
        "service_tier": trusted.service_tier,
        "status": trusted.status,
        "total_tokens": trusted.total_tokens,
    }


def _parse_history_transport_binding_projection(
    value: object,
) -> OpenAIResponsesTransportBindingV1:
    expected = set(OpenAIResponsesTransportBindingV1.__dataclass_fields__)
    if type(value) is not dict or set(value) != expected:
        raise R24ContractError("INVALID_HISTORY_REQUEST_PROOF", "history transport fields differ")
    projection = cast(dict[str, object], value)
    try:
        trusted: OpenAIResponsesTransportBindingV1 = cast(Any, OpenAIResponsesTransportBindingV1)(
            **{name: projection[name] for name in expected}
        )
    except Exception as exc:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history transport binding is invalid"
        ) from exc
    if canonical_json_bytes(cast(JsonValue, projection)) != canonical_json_bytes(
        cast(JsonValue, openai_responses_transport_binding_projection(trusted))
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history transport binding is non-canonical"
        )
    return trusted


def _parse_history_response_envelope_projection(
    value: object,
) -> ResponsesEnvelopeV1:
    expected = {
        "input_tokens",
        "output_text",
        "output_tokens",
        "requested_model",
        "response_id",
        "returned_model",
        "schema_version",
        "service_tier",
        "status",
        "total_tokens",
    }
    if type(value) is not dict or set(value) != expected:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history response envelope fields differ"
        )
    projection = cast(dict[str, object], value)
    try:
        envelope = ResponsesEnvelopeV1(
            response_id=cast(str, projection["response_id"]),
            requested_model=cast(str, projection["requested_model"]),
            returned_model=cast(str, projection["returned_model"]),
            status=cast(str, projection["status"]),
            service_tier=cast(str | None, projection["service_tier"]),
            output_text=cast(str, projection["output_text"]),
            input_tokens=cast(int | None, projection["input_tokens"]),
            output_tokens=cast(int | None, projection["output_tokens"]),
            total_tokens=cast(int | None, projection["total_tokens"]),
            schema_version=cast(str, projection["schema_version"]),
        )
    except Exception as exc:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history response envelope is invalid"
        ) from exc
    if canonical_json_bytes(cast(JsonValue, projection)) != canonical_json_bytes(
        cast(JsonValue, _history_response_envelope_projection(envelope))
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history response envelope is non-canonical"
        )
    return envelope


def _parse_r22_policy_receipt_projection(value: object) -> R22PolicyReceiptV1:
    expected = set(R22PolicyReceiptV1.__dataclass_fields__)
    if type(value) is not dict or set(value) != expected:
        raise R24ContractError("INVALID_HISTORY_REQUEST_PROOF", "R2.2 policy receipt fields differ")
    projection = cast(dict[str, object], value)
    arguments = dict(projection)
    try:
        arguments["evaluation_status"] = PolicyEvaluationStatus(
            cast(str, arguments["evaluation_status"])
        )
        checks = arguments["validation_checks"]
        if type(checks) is not list or any(type(item) is not str for item in checks):
            raise TypeError("R2.2 validation checks differ")
        arguments["validation_checks"] = tuple(checks)
        receipt: R22PolicyReceiptV1 = cast(Any, R22PolicyReceiptV1)(**arguments)
    except Exception as exc:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "R2.2 policy receipt is invalid"
        ) from exc
    if canonical_json_bytes(cast(JsonValue, projection)) != canonical_json_bytes(
        cast(JsonValue, r22_policy_receipt_dict(receipt))
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "R2.2 policy receipt is non-canonical"
        )
    return receipt


def _history_receipt_exact_cost_matches_pricing(
    receipt: LiveAttemptReceiptV1,
    pricing: LiveAttemptPricingV1,
) -> bool:
    if receipt.cost_status is not LiveAttemptCostStatusV1.EXACT:
        return receipt.cost_usd_micros is None
    if receipt.dispatch_count == 0:
        return receipt.cost_usd_micros == 0
    if any(
        value is None
        for value in (
            receipt.input_tokens,
            receipt.cached_input_tokens,
            receipt.output_tokens,
        )
    ):
        return False
    return receipt.cost_usd_micros == live_attempt_cost_usd_micros(
        pricing,
        input_tokens=cast(int, receipt.input_tokens),
        cached_input_tokens=cast(int, receipt.cached_input_tokens),
        output_tokens=cast(int, receipt.output_tokens),
    )


def _history_receipt_authority_limits_are_valid(
    receipt: LiveAttemptReceiptV1,
    authority: LiveAttemptAuthorityV1,
) -> bool:
    exceeds = (
        receipt.cost_usd_micros is not None
        and receipt.cost_usd_micros > authority.max_cost_usd_micros
    ) or (receipt.output_tokens is not None and receipt.output_tokens > authority.max_output_tokens)
    if receipt.failure_code == "PROVIDER_RESULT_EXCEEDS_AUTHORITY":
        return receipt.status is LiveAttemptStatusV1.FAILED and exceeds
    if not exceeds:
        return True
    return (
        receipt.status
        in {
            LiveAttemptStatusV1.FAILED,
            LiveAttemptStatusV1.CANCELLED_POST_DISPATCH,
            LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
        }
        and receipt.late_output_detected
        and receipt.response_envelope_sha256 is not None
        and receipt.cost_status is LiveAttemptCostStatusV1.EXACT
        and receipt.cost_usd_micros is not None
        and all(
            value is not None
            for value in (
                receipt.input_tokens,
                receipt.cached_input_tokens,
                receipt.output_tokens,
                receipt.total_tokens,
            )
        )
    )


@dataclass(frozen=True, slots=True)
class LiveHistoryPolicyAttemptRequestAnchorV1:
    """Module-sealed full preimage for one HISTORY_POLICY attempt."""

    attempt_id: str
    logical_call_id: str
    actor_request_sha256: str
    coordinator_evidence_packet_sha256: str
    attempt_authority: LiveAttemptAuthorityV1 = field(repr=False)
    deadline_binding: LiveAttemptDeadlineBindingV1 = field(repr=False)
    constraint_binding: LiveRubricAttemptConstraintBindingV1 = field(repr=False)
    case_execution_lease: dict[str, JsonValue] = field(repr=False)
    openai_stage: OpenAIResponsesStageV1 = field(repr=False)
    rubric_openai_stage: OpenAIResponsesStageV1 = field(repr=False)
    pricing: LiveAttemptPricingV1 = field(repr=False)
    transport_binding: OpenAIResponsesTransportBindingV1 = field(repr=False)
    provider_request: CanonicalHistoryPolicyRequestV1 = field(repr=False)
    response_envelope: ResponsesEnvelopeV1 | None = field(repr=False)
    r22_policy_receipt: R22PolicyReceiptV1 | None = field(repr=False)
    r22_receipt_state: LiveHistoryPolicyReceiptStateV1
    r22_receipt_failure_code: str | None
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _HISTORY_REQUEST_PROOF_SEAL:
            raise R24ContractError(
                "UNTRUSTED_HISTORY_REQUEST_PROOF",
                "history request proof was not issued by production orchestration",
            )
        _require_id(self.attempt_id, "attempt_id")
        _require_id(self.logical_call_id, "logical_call_id")
        _require_sha256(self.actor_request_sha256, "actor_request_sha256")
        _require_sha256(
            self.coordinator_evidence_packet_sha256,
            "coordinator_evidence_packet_sha256",
        )
        if type(self.r22_receipt_state) is not LiveHistoryPolicyReceiptStateV1:
            raise R24ContractError(
                "UNTRUSTED_HISTORY_REQUEST_PROOF", "R2.2 receipt state type differs"
            )
        if self.r22_receipt_failure_code is not None and (
            type(self.r22_receipt_failure_code) is not str
            or self.r22_receipt_failure_code not in _R22_RECEIPT_PUBLICATION_FAILURE_CODES
        ):
            raise R24ContractError(
                "UNTRUSTED_HISTORY_REQUEST_PROOF",
                "history R2.2 receipt failure code is not a closed publication failure",
            )
        try:
            authority = snapshot_live_attempt_authority(self.attempt_authority)
            deadline = snapshot_live_attempt_deadline_binding(self.deadline_binding)
            constraint = snapshot_live_rubric_attempt_constraint_binding(self.constraint_binding)
            lease = parse_durable_case_execution_lease_projection_v1(self.case_execution_lease)
            stage = parse_durable_openai_stage_projection_v1(
                openai_stage_projection(self.openai_stage)
            )
            rubric_stage = parse_durable_openai_stage_projection_v1(
                openai_stage_projection(self.rubric_openai_stage)
            )
            pricing = snapshot_live_attempt_pricing(self.pricing)
            transport = _snapshot_history_transport_binding(self.transport_binding)
            request = snapshot_canonical_history_policy_request(self.provider_request)
            envelope = (
                None
                if self.response_envelope is None
                else _snapshot_history_response_envelope(self.response_envelope)
            )
            r22_receipt = (
                None
                if self.r22_policy_receipt is None
                else detach_r22_policy_receipt(self.r22_policy_receipt)
            )
        except Exception as exc:
            raise R24ContractError(
                "UNTRUSTED_HISTORY_REQUEST_PROOF",
                "history request proof preimages could not be detached",
            ) from exc
        committed = self.r22_receipt_state is LiveHistoryPolicyReceiptStateV1.R22_RECEIPT_COMMITTED
        if committed != (r22_receipt is not None):
            raise R24ContractError(
                "UNTRUSTED_HISTORY_REQUEST_PROOF",
                "history R2.2 receipt state differs from its preimage",
            )
        if committed and self.r22_receipt_failure_code is not None:
            raise R24ContractError(
                "UNTRUSTED_HISTORY_REQUEST_PROOF",
                "a committed history R2.2 receipt cannot also be publication-failed",
            )
        object.__setattr__(self, "attempt_authority", authority)
        object.__setattr__(self, "deadline_binding", deadline)
        object.__setattr__(self, "constraint_binding", constraint)
        object.__setattr__(self, "case_execution_lease", lease)
        object.__setattr__(self, "openai_stage", stage)
        object.__setattr__(self, "rubric_openai_stage", rubric_stage)
        object.__setattr__(self, "pricing", pricing)
        object.__setattr__(self, "transport_binding", transport)
        object.__setattr__(self, "provider_request", request)
        object.__setattr__(self, "response_envelope", envelope)
        object.__setattr__(self, "r22_policy_receipt", r22_receipt)


def _build_live_history_policy_attempt_request_anchor_v1(
    *,
    attempt_id: str,
    logical_call_id: str,
    actor_request_sha256: str,
    coordinator_evidence_packet_sha256: str,
    attempt_authority: LiveAttemptAuthorityV1,
    deadline_binding: LiveAttemptDeadlineBindingV1,
    constraint_binding: LiveRubricAttemptConstraintBindingV1,
    case_execution_lease: CaseExecutionLeaseV1,
    openai_stage: OpenAIResponsesStageV1,
    rubric_openai_stage: OpenAIResponsesStageV1,
    pricing: LiveAttemptPricingV1,
    transport_binding: OpenAIResponsesTransportBindingV1,
    provider_request: CanonicalHistoryPolicyRequestV1,
    response_envelope: ResponsesEnvelopeV1 | None,
    r22_policy_receipt: R22PolicyReceiptV1 | None,
    r22_receipt_state: LiveHistoryPolicyReceiptStateV1,
    r22_receipt_failure_code: str | None = None,
) -> LiveHistoryPolicyAttemptRequestAnchorV1:
    return LiveHistoryPolicyAttemptRequestAnchorV1(
        attempt_id=attempt_id,
        logical_call_id=logical_call_id,
        actor_request_sha256=actor_request_sha256,
        coordinator_evidence_packet_sha256=coordinator_evidence_packet_sha256,
        attempt_authority=attempt_authority,
        deadline_binding=deadline_binding,
        constraint_binding=constraint_binding,
        case_execution_lease=case_execution_lease_projection(case_execution_lease),
        openai_stage=openai_stage,
        rubric_openai_stage=rubric_openai_stage,
        pricing=pricing,
        transport_binding=transport_binding,
        provider_request=provider_request,
        response_envelope=response_envelope,
        r22_policy_receipt=r22_policy_receipt,
        r22_receipt_state=r22_receipt_state,
        r22_receipt_failure_code=r22_receipt_failure_code,
        _seal=_HISTORY_REQUEST_PROOF_SEAL,
    )


def snapshot_live_history_policy_attempt_request_anchor(
    value: LiveHistoryPolicyAttemptRequestAnchorV1,
) -> LiveHistoryPolicyAttemptRequestAnchorV1:
    if type(value) is not LiveHistoryPolicyAttemptRequestAnchorV1:
        raise R24ContractError(
            "UNTRUSTED_HISTORY_REQUEST_PROOF", "history request anchor type differs"
        )
    return LiveHistoryPolicyAttemptRequestAnchorV1(
        attempt_id=value.attempt_id,
        logical_call_id=value.logical_call_id,
        actor_request_sha256=value.actor_request_sha256,
        coordinator_evidence_packet_sha256=(value.coordinator_evidence_packet_sha256),
        attempt_authority=value.attempt_authority,
        deadline_binding=value.deadline_binding,
        constraint_binding=value.constraint_binding,
        case_execution_lease=value.case_execution_lease,
        openai_stage=value.openai_stage,
        rubric_openai_stage=value.rubric_openai_stage,
        pricing=value.pricing,
        transport_binding=value.transport_binding,
        provider_request=value.provider_request,
        response_envelope=value.response_envelope,
        r22_policy_receipt=value.r22_policy_receipt,
        r22_receipt_state=value.r22_receipt_state,
        r22_receipt_failure_code=value.r22_receipt_failure_code,
        _seal=_HISTORY_REQUEST_PROOF_SEAL,
    )


def _validate_history_provider_request_v1(
    request: CanonicalHistoryPolicyRequestV1,
    *,
    transport_binding: OpenAIResponsesTransportBindingV1,
    logical_call_id: str,
    actor_request_sha256: str,
) -> tuple[str, str, str, str, str, str, str]:
    """Rebuild R2.2 semantics plus the exact R2.4 physical request provenance."""

    projection = _history_provider_request_projection(request)
    expected_fields = {
        "input",
        "instructions",
        "max_output_tokens",
        "model",
        "parallel_tool_calls",
        "reasoning",
        "store",
        "stream",
        "text",
        "tool_choice",
        "tools",
        "truncation",
    }
    input_value = projection.get("input")
    if type(input_value) is not list or len(input_value) != 1:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history provider input envelope differs"
        )
    message = input_value[0]
    if type(message) is not dict or set(message) != {"content", "role"}:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history provider input message differs"
        )
    content = message.get("content")
    if (
        message.get("role") != "user"
        or type(message.get("role")) is not str
        or type(content) is not list
        or len(content) != 2
    ):
        raise R24ContractError("INVALID_HISTORY_REQUEST_PROOF", "history provider content differs")
    text_part, image_part = content
    if (
        type(text_part) is not dict
        or set(text_part) != {"text", "type"}
        or text_part.get("type") != "input_text"
        or type(text_part.get("text")) is not str
        or type(image_part) is not dict
        or set(image_part) != {"detail", "image_url", "type"}
        or image_part.get("type") != "input_image"
        or image_part.get("detail") != "high"
        or type(image_part.get("image_url")) is not str
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history provider semantic content differs"
        )
    packet_text = cast(str, text_part["text"])
    try:
        packet = json.loads(packet_text)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history evidence packet is invalid JSON"
        ) from exc
    if (
        type(packet) is not dict
        or canonical_json_bytes(cast(JsonValue, packet)).decode("utf-8") != packet_text
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history evidence packet is not canonical"
        )
    if tuple(_history_evidence_packet_validator().iter_errors(packet)):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF",
            "history evidence packet does not match the accepted R2.2 schema",
        )
    packet_id = packet.get("packet_id")
    packet_host_id = packet.get("host_id")
    if (
        type(packet_id) is not str
        or type(packet_host_id) is not str
        or packet.get("logical_call_id") != logical_call_id
        or packet.get("raw_request_sha256") != actor_request_sha256
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF",
            "history evidence packet differs from its actor call",
        )
    packet_sha256 = hashlib.sha256(packet_text.encode("utf-8")).hexdigest()
    image_url = cast(str, image_part["image_url"])
    match = _HISTORY_DATA_IMAGE.fullmatch(image_url)
    if match is None:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history current image URL is invalid"
        )
    try:
        image_bytes = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "history current image is invalid"
        ) from exc
    if not image_bytes:
        raise R24ContractError("INVALID_HISTORY_REQUEST_PROOF", "history current image is empty")
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    current = packet.get("current_observation")
    if (
        type(current) is not dict
        or current.get("screenshot_content_sha256") != image_sha256
        or current.get("media_type") != match.group(1)
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF",
            "history image differs from its evidence-packet current observation",
        )
    checked_schema = ProposalSchemaSnapshotV1.from_checked_in()
    transport_schema = history_policy_transport_schema_v1()
    expected_request: dict[str, JsonValue] = {
        "input": cast(JsonValue, input_value),
        "instructions": GPT56_POLICY_INSTRUCTIONS,
        "max_output_tokens": GPT56_MAX_OUTPUT_TOKENS,
        "model": GPT56_REQUESTED_MODEL,
        "parallel_tool_calls": False,
        "reasoning": {"effort": GPT56_REASONING_EFFORT},
        "store": False,
        "stream": False,
        "text": {
            "format": {
                "name": transport_schema.name,
                "schema": cast(JsonValue, transport_schema.as_dict()),
                "strict": True,
                "type": "json_schema",
            },
            "verbosity": "low",
        },
        "tool_choice": "none",
        "tools": [],
        "truncation": "disabled",
    }
    if set(projection) != expected_fields or canonical_json_bytes(
        cast(JsonValue, projection)
    ) != canonical_json_bytes(cast(JsonValue, expected_request)):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF",
            "history provider request differs from the fixed R2.4 transport request",
        )
    config: dict[str, JsonValue] = {
        "schema_version": "mobileworld.runtime.sentinel-gpt56-request/v1",
        "model": GPT56_REQUESTED_MODEL,
        "prompt_sha256": hashlib.sha256(GPT56_POLICY_INSTRUCTIONS.encode("utf-8")).hexdigest(),
        "reasoning_effort": GPT56_REASONING_EFFORT,
        "output_schema_name": GPT56_OUTPUT_SCHEMA_NAME,
        "output_schema_sha256": checked_schema.sha256,
        "text_verbosity": "low",
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "store": False,
        "stream": False,
        "truncation": "disabled",
        "max_output_tokens": GPT56_MAX_OUTPUT_TOKENS,
        "image_detail": "high",
        "temperature_supplied": False,
        "reasoning_summary_requested": False,
        "transport_timeout_ns": transport_binding.transport_timeout_ns,
        "seam_policy_deadline_ns": transport_binding.seam_policy_deadline_ns,
        "sdk_max_retries": 0,
        "openai_sdk_version": SUPPORTED_OPENAI_SDK_VERSION,
    }
    return (
        packet_sha256,
        image_sha256,
        canonical_sha256(config),
        packet_id,
        packet_host_id,
        transport_schema.sha256,
        checked_schema.sha256,
    )


def validate_live_history_policy_attempt_request_anchor_v1(
    value: LiveHistoryPolicyAttemptRequestAnchorV1,
    *,
    attempt_receipt: LiveAttemptReceiptV1,
) -> None:
    """Cross-bind one history authority to every independent request source."""

    anchor = snapshot_live_history_policy_attempt_request_anchor(value)
    receipt = snapshot_live_attempt_receipt(attempt_receipt)
    authority = anchor.attempt_authority
    deadline = anchor.deadline_binding
    constraint = anchor.constraint_binding
    lease = anchor.case_execution_lease
    stage = anchor.openai_stage
    rubric_stage = anchor.rubric_openai_stage
    pricing = anchor.pricing
    transport = anchor.transport_binding
    request = anchor.provider_request
    (
        packet_sha256,
        image_sha256,
        request_config_sha256,
        packet_id,
        packet_host_id,
        _provider_output_schema_sha256,
        r22_output_schema_sha256,
    ) = _validate_history_provider_request_v1(
        request,
        transport_binding=transport,
        logical_call_id=anchor.logical_call_id,
        actor_request_sha256=anchor.actor_request_sha256,
    )
    authority_sha256 = live_attempt_authority_sha256(authority)
    lease_sha256 = canonical_sha256(cast(JsonValue, lease))
    stage_sha256 = openai_stage_sha256(stage)
    pricing_sha256 = live_attempt_pricing_sha256(pricing)
    transport_sha256 = openai_responses_transport_binding_sha256(transport)
    cost_reservation_failure = (
        receipt.status is LiveAttemptStatusV1.FAILED
        and receipt.dispatch_count == 0
        and receipt.failure_code == "ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY"
    )
    if (
        receipt.role is not LiveAttemptRoleV1.HISTORY_POLICY
        or authority.role is not LiveAttemptRoleV1.HISTORY_POLICY
        or stage.role is not OpenAIRoleV1.HISTORY_POLICY
        or rubric_stage.role is not OpenAIRoleV1.RUBRIC
        or receipt.attempt_id != anchor.attempt_id
        or authority.attempt_id != anchor.attempt_id
        or deadline.attempt_id != anchor.attempt_id
        or receipt.logical_call_id != anchor.logical_call_id
        or authority.logical_call_id != anchor.logical_call_id
        or deadline.logical_call_id != anchor.logical_call_id
        or receipt.actor_request_sha256 != anchor.actor_request_sha256
        or authority.actor_request_sha256 != anchor.actor_request_sha256
        or lease.get("request_sha256") != anchor.actor_request_sha256
        or receipt.authority_sha256 != authority_sha256
        or receipt.manifest_sha256 != authority.manifest_sha256
        or lease.get("manifest_sha256") != authority.manifest_sha256
        or receipt.preflight_sha256 != authority.preflight_sha256
        or lease.get("preflight_report_sha256") != authority.preflight_sha256
        or receipt.case_execution_lease_sha256 != lease_sha256
        or authority.case_execution_lease_sha256 != lease_sha256
        or deadline.case_execution_lease_sha256 != lease_sha256
        or receipt.case_id != authority.case_id
        or deadline.case_id != authority.case_id
        or lease.get("case_id") != authority.case_id
        or receipt.stage_sha256 != stage_sha256
        or authority.stage_sha256 != stage_sha256
        or receipt.pricing_binding_sha256 != pricing_sha256
        or authority.pricing_binding_sha256 != pricing_sha256
        or lease.get("pricing_binding_sha256") != pricing_sha256
        or receipt.transport_binding_sha256 != transport_sha256
        or authority.transport_binding_sha256 != transport_sha256
        or receipt.request_sha256 != request.request_sha256
        or authority.request_sha256 != request.request_sha256
        or authority.deadline_monotonic_ns != deadline.effective_deadline_monotonic_ns
        or authority.max_cost_usd_micros != deadline.max_cost_usd_micros
        or authority.max_cost_usd_micros != constraint.attempt_max_cost_usd_micros
        or deadline.case_execution_deadline_monotonic_ns
        != constraint.effective_deadline_monotonic_ns
        or deadline.request_timeout_ns != transport.transport_timeout_ns
        or deadline.constraint_registered_monotonic_ns < constraint.issued_monotonic_ns
        or authority.max_output_tokens != GPT56_MAX_OUTPUT_TOKENS
        or stage.max_output_tokens != GPT56_MAX_OUTPUT_TOKENS
        or transport.max_output_tokens != GPT56_MAX_OUTPUT_TOKENS
        or stage != constraint.history_stage
        or constraint.rubric_stage_sha256 != openai_stage_sha256(rubric_stage)
        or constraint.rubric_stage_timeout_ms != rubric_stage.timeout_ms
        or lease.get("openai_stage_set_sha256") != openai_stage_set_sha256((rubric_stage, stage))
        or lease.get("stage") != constraint.case_stage
        or lease.get("host") != constraint.case_host
        or lease.get("mode") != constraint.case_mode
        or lease.get("case_id") != constraint.case_id
        or lease.get("task_id") != constraint.task_id
        or lease.get("task_parameters_sha256") != constraint.task_parameters_sha256
        or lease.get("reset_seed") != constraint.reset_seed
        or type(lease.get("actor_call_index")) is not int
        or cast(int, lease["actor_call_index"]) > constraint.max_actor_calls
        or stage.max_attempts != 1
        or receipt.dispatch_count > stage.max_attempts
        or not _history_receipt_exact_cost_matches_pricing(receipt, pricing)
        or not _history_receipt_authority_limits_are_valid(receipt, authority)
        or transport.case_execution_lease_sha256 != lease_sha256
        or transport.history_policy_stage_sha256 != stage_sha256
        or transport.authority_manifest_sha256 != authority.manifest_sha256
        or transport.preflight_report_sha256 != authority.preflight_sha256
        or transport.actor_request_sha256 != anchor.actor_request_sha256
        or transport.pricing_binding_sha256 != pricing_sha256
        or transport.factory_binding_sha256 != lease.get("factory_binding_sha256")
        or transport.descriptor_sha256
        != transport_descriptor_sha256(
            TransportDescriptorV1(
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version=SUPPORTED_OPENAI_SDK_VERSION,
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
            )
        )
        or (
            live_attempt_worst_case_cost_usd_micros(
                pricing,
                request_byte_count=request.byte_count,
                max_output_tokens=authority.max_output_tokens,
            )
            > authority.max_cost_usd_micros
        )
        is not cost_reservation_failure
        or packet_sha256 != anchor.coordinator_evidence_packet_sha256
    ):
        raise R24ContractError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "history authority differs from deadline, request, lease, stage, pricing, or transport",
        )
    r22 = anchor.r22_policy_receipt
    if receipt.status is not LiveAttemptStatusV1.COMPLETED and r22 is not None:
        response_or_semantic_values = (
            r22.returned_model,
            r22.response_id,
            r22.response_status,
            r22.service_tier,
            r22.response_envelope_sha256,
            r22.provider_output_sha256,
            r22.parsed_proposal_sha256,
            r22.admitted_plan_sha256,
            r22.input_tokens,
            r22.output_tokens,
            r22.total_tokens,
        )
        decision_census = (
            r22.decision_count,
            r22.keep_count,
            r22.drop_count,
            r22.replace_count,
            r22.keep_uncertain_count,
            r22.material_decision_count,
            r22.abstain_decision_count,
        )
        if (
            receipt.dispatch_count != 1
            or r22.transport_calls != 1
            or r22.evaluation_status is not PolicyEvaluationStatus.TRANSPORT_ERROR
            or r22.failure_code != "POLICY_TRANSPORT_ERROR"
            or any(item is not None for item in response_or_semantic_values)
            or any(decision_census)
        ):
            raise R24ContractError(
                "HISTORY_REQUEST_PROOF_MISMATCH",
                "a non-completed history attempt can bind only a pre-response R2.2 transport-error receipt",
            )
    envelope = anchor.response_envelope
    if (receipt.response_envelope_sha256 is None) != (envelope is None):
        raise R24ContractError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "history response-envelope presence differs from the attempt",
        )
    if receipt.status is LiveAttemptStatusV1.COMPLETED and envelope is None:
        raise R24ContractError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "completed history attempt lacks its provider response",
        )
    if envelope is not None and (
        receipt.response_envelope_sha256
        != canonical_sha256(cast(JsonValue, responses_envelope_hash_projection(envelope)))
        or receipt.requested_model != envelope.requested_model
        or receipt.returned_model != envelope.returned_model
        or receipt.input_tokens != envelope.input_tokens
        or receipt.output_tokens != envelope.output_tokens
        or receipt.total_tokens != envelope.total_tokens
    ):
        raise R24ContractError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "history provider response differs from the attempt receipt",
        )
    if r22 is None:
        if receipt.status is LiveAttemptStatusV1.COMPLETED and (
            receipt.dispatch_count != 1
            or anchor.r22_receipt_failure_code not in _R22_RECEIPT_PUBLICATION_FAILURE_CODES
        ):
            raise R24ContractError(
                "HISTORY_REQUEST_PROOF_MISMATCH",
                "completed history attempt lacks both an R2.2 receipt and a publication failure",
            )
        expected_state = (
            LiveHistoryPolicyReceiptStateV1.R22_RECEIPT_ABSENT_PRE_DISPATCH
            if receipt.dispatch_count == 0
            else LiveHistoryPolicyReceiptStateV1.R22_RECEIPT_ABSENT_POST_DISPATCH
        )
        if anchor.r22_receipt_state is not expected_state:
            raise R24ContractError(
                "HISTORY_REQUEST_PROOF_MISMATCH",
                "history missing-receipt state differs from dispatch state",
            )
        return
    if (
        anchor.r22_receipt_state is not LiveHistoryPolicyReceiptStateV1.R22_RECEIPT_COMMITTED
        or r22.logical_call_id != anchor.logical_call_id
        or r22.packet_id != packet_id
        or r22.host_id != packet_host_id
        or r22.requested_model != GPT56_REQUESTED_MODEL
        or r22.openai_sdk_version != SUPPORTED_OPENAI_SDK_VERSION
        or r22.sdk_max_retries != 0
        or r22.transport_kind != "OPENAI_RESPONSES"
        or r22.transport_authority != "EXPLICIT_OWNER_AUTHORIZATION"
        or r22.prompt_sha256
        != hashlib.sha256(GPT56_POLICY_INSTRUCTIONS.encode("utf-8")).hexdigest()
        or r22.output_schema_sha256 != r22_output_schema_sha256
        or r22.request_config_sha256 != request_config_sha256
        or r22.evidence_packet_sha256 != packet_sha256
        or r22.current_image_sha256 != image_sha256
        or r22.transport_calls != receipt.dispatch_count
        or r22.external_network_attempted != (receipt.dispatch_count == 1)
        or r22.model_call_attempted != (receipt.dispatch_count == 1)
    ):
        raise R24ContractError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "R2.2 receipt differs from the canonical history provider request",
        )
    if r22.response_envelope_sha256 is not None and (
        envelope is None
        or r22.response_envelope_sha256
        != canonical_sha256(cast(JsonValue, responses_envelope_hash_projection(envelope)))
        or r22.provider_output_sha256 != envelope.output_text_sha256
        or r22.returned_model != envelope.returned_model
        or r22.input_tokens != envelope.input_tokens
        or r22.output_tokens != envelope.output_tokens
        or r22.total_tokens != envelope.total_tokens
    ):
        raise R24ContractError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "R2.2 provider-output provenance differs from its envelope",
        )
    if r22.response_envelope_sha256 is None and any(
        value is not None
        for value in (
            r22.provider_output_sha256,
            r22.returned_model,
            r22.input_tokens,
            r22.output_tokens,
            r22.total_tokens,
        )
    ):
        raise R24ContractError(
            "HISTORY_REQUEST_PROOF_MISMATCH",
            "R2.2 receipt contains partial provider-output provenance",
        )


def live_history_policy_attempt_request_proof_projection(
    value: LiveHistoryPolicyAttemptRequestAnchorV1,
    *,
    attempt_receipt: LiveAttemptReceiptV1,
) -> dict[str, JsonValue]:
    """Project the complete owner-only durable history attempt proof."""

    anchor = snapshot_live_history_policy_attempt_request_anchor(value)
    receipt = snapshot_live_attempt_receipt(attempt_receipt)
    validate_live_history_policy_attempt_request_anchor_v1(anchor, attempt_receipt=receipt)
    provider_schema = history_policy_transport_schema_v1()
    r22_schema = ProposalSchemaSnapshotV1.from_checked_in()
    proof: dict[str, JsonValue] = {
        "actor_request_sha256": anchor.actor_request_sha256,
        "attempt_authority": cast(
            JsonValue, live_attempt_authority_projection(anchor.attempt_authority)
        ),
        "attempt_authority_sha256": live_attempt_authority_sha256(anchor.attempt_authority),
        "attempt_dispatch_count": receipt.dispatch_count,
        "attempt_id": anchor.attempt_id,
        "attempt_receipt_sha256": live_attempt_receipt_sha256(receipt),
        "attempt_role": receipt.role.value,
        "attempt_status": receipt.status.value,
        "case_execution_lease": cast(JsonValue, anchor.case_execution_lease),
        "constraint_binding": cast(
            JsonValue,
            live_rubric_attempt_constraint_binding_projection(anchor.constraint_binding),
        ),
        "coordinator_evidence_packet_sha256": (anchor.coordinator_evidence_packet_sha256),
        "deadline_binding": cast(
            JsonValue,
            live_attempt_deadline_binding_projection(anchor.deadline_binding),
        ),
        "logical_call_id": anchor.logical_call_id,
        "openai_stage": cast(JsonValue, openai_stage_projection(anchor.openai_stage)),
        "rubric_openai_stage": cast(JsonValue, openai_stage_projection(anchor.rubric_openai_stage)),
        "pricing": cast(JsonValue, live_attempt_pricing_projection(anchor.pricing)),
        "provider_output_schema_sha256": provider_schema.sha256,
        "provider_request": cast(
            JsonValue, _history_provider_request_projection(anchor.provider_request)
        ),
        "provider_request_byte_count": anchor.provider_request.byte_count,
        "provider_request_sha256": anchor.provider_request.request_sha256,
        "r22_output_schema_sha256": r22_schema.sha256,
        "r22_policy_receipt": (
            None
            if anchor.r22_policy_receipt is None
            else cast(JsonValue, r22_policy_receipt_dict(anchor.r22_policy_receipt))
        ),
        "r22_policy_receipt_sha256": (
            None if anchor.r22_policy_receipt is None else anchor.r22_policy_receipt.sha256
        ),
        "r22_receipt_failure_code": anchor.r22_receipt_failure_code,
        "r22_receipt_state": anchor.r22_receipt_state.value,
        "response_envelope": (
            None
            if anchor.response_envelope is None
            else cast(
                JsonValue,
                _history_response_envelope_projection(anchor.response_envelope),
            )
        ),
        "schema_version": LIVE_HISTORY_POLICY_REQUEST_PROOF_SCHEMA_VERSION,
        "transport_binding": cast(
            JsonValue,
            openai_responses_transport_binding_projection(anchor.transport_binding),
        ),
    }
    validate_live_history_policy_request_proof_projection_v1(
        cast(JsonValue, proof),
        attempt_receipt=receipt,
        expected_attempt_authority_sha256=live_attempt_authority_sha256(anchor.attempt_authority),
        expected_constraint_binding_sha256=canonical_sha256(
            cast(
                JsonValue,
                live_rubric_attempt_constraint_binding_projection(anchor.constraint_binding),
            )
        ),
        expected_manifest_sha256=anchor.attempt_authority.manifest_sha256,
        expected_preflight_sha256=anchor.attempt_authority.preflight_sha256,
        expected_case_execution_lease_sha256=(anchor.attempt_authority.case_execution_lease_sha256),
        expected_stage_sha256=anchor.attempt_authority.stage_sha256,
        expected_pricing_binding_sha256=anchor.attempt_authority.pricing_binding_sha256,
        expected_transport_binding_sha256=(anchor.attempt_authority.transport_binding_sha256),
        expected_request_sha256=anchor.attempt_authority.request_sha256,
    )
    return proof


def validate_live_history_policy_request_proof_projection_v1(
    value: JsonValue,
    *,
    attempt_receipt: LiveAttemptReceiptV1 | dict[str, JsonValue],
    expected_attempt_authority_sha256: str,
    expected_constraint_binding_sha256: str,
    expected_manifest_sha256: str,
    expected_preflight_sha256: str,
    expected_case_execution_lease_sha256: str,
    expected_stage_sha256: str,
    expected_pricing_binding_sha256: str,
    expected_transport_binding_sha256: str,
    expected_request_sha256: str,
) -> None:
    """Independently rebuild a durable HISTORY_POLICY proof graph."""

    try:
        schema_errors = tuple(_history_request_proof_validator().iter_errors(value))
    except Exception as exc:
        if isinstance(exc, R24ContractError):
            raise
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF",
            "durable history proof schema evaluation failed",
        ) from exc
    if schema_errors:
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF",
            "durable history proof does not match its checked-in schema",
        )

    expected_fields = {
        "actor_request_sha256",
        "attempt_authority",
        "attempt_authority_sha256",
        "attempt_dispatch_count",
        "attempt_id",
        "attempt_receipt_sha256",
        "attempt_role",
        "attempt_status",
        "case_execution_lease",
        "constraint_binding",
        "coordinator_evidence_packet_sha256",
        "deadline_binding",
        "logical_call_id",
        "openai_stage",
        "rubric_openai_stage",
        "pricing",
        "provider_output_schema_sha256",
        "provider_request",
        "provider_request_byte_count",
        "provider_request_sha256",
        "r22_output_schema_sha256",
        "r22_policy_receipt",
        "r22_policy_receipt_sha256",
        "r22_receipt_failure_code",
        "r22_receipt_state",
        "response_envelope",
        "schema_version",
        "transport_binding",
    }
    if (
        type(value) is not dict
        or set(value) != expected_fields
        or value.get("schema_version") != LIVE_HISTORY_POLICY_REQUEST_PROOF_SCHEMA_VERSION
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "durable history proof fields differ"
        )
    proof = value
    try:
        authority = parse_live_attempt_authority_projection(proof["attempt_authority"])
        deadline = parse_live_attempt_deadline_binding_projection(proof["deadline_binding"])
        constraint = parse_live_rubric_attempt_constraint_binding_projection_v1(
            proof["constraint_binding"]
        )
        lease = parse_durable_case_execution_lease_projection_v1(proof["case_execution_lease"])
        stage = parse_durable_openai_stage_projection_v1(proof["openai_stage"])
        rubric_stage = parse_durable_openai_stage_projection_v1(proof["rubric_openai_stage"])
        pricing = parse_durable_attempt_pricing_projection_v1(proof["pricing"])
        transport = _parse_history_transport_binding_projection(proof["transport_binding"])
        request_projection = proof["provider_request"]
        if type(request_projection) is not dict:
            raise TypeError("provider request projection differs")
        request = build_canonical_history_policy_request(
            cast(dict[str, object], request_projection)
        )
        envelope = (
            None
            if proof["response_envelope"] is None
            else _parse_history_response_envelope_projection(proof["response_envelope"])
        )
        r22 = (
            None
            if proof["r22_policy_receipt"] is None
            else _parse_r22_policy_receipt_projection(proof["r22_policy_receipt"])
        )
        receipt_state = LiveHistoryPolicyReceiptStateV1(cast(str, proof["r22_receipt_state"]))
        receipt_failure_code = cast(str | None, proof["r22_receipt_failure_code"])
        if type(attempt_receipt) is LiveAttemptReceiptV1:
            receipt = snapshot_live_attempt_receipt(attempt_receipt)
        else:
            receipt = parse_durable_live_attempt_receipt_projection_v1(
                cast(dict[str, JsonValue], attempt_receipt)
            )
        anchor = LiveHistoryPolicyAttemptRequestAnchorV1(
            attempt_id=cast(str, proof["attempt_id"]),
            logical_call_id=cast(str, proof["logical_call_id"]),
            actor_request_sha256=cast(str, proof["actor_request_sha256"]),
            coordinator_evidence_packet_sha256=cast(
                str, proof["coordinator_evidence_packet_sha256"]
            ),
            attempt_authority=authority,
            deadline_binding=deadline,
            constraint_binding=constraint,
            case_execution_lease=lease,
            openai_stage=stage,
            rubric_openai_stage=rubric_stage,
            pricing=pricing,
            transport_binding=transport,
            provider_request=request,
            response_envelope=envelope,
            r22_policy_receipt=r22,
            r22_receipt_state=receipt_state,
            r22_receipt_failure_code=receipt_failure_code,
            _seal=_HISTORY_REQUEST_PROOF_SEAL,
        )
    except Exception as exc:
        if isinstance(exc, R24ContractError):
            raise
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF", "durable history proof is invalid"
        ) from exc
    if (
        proof["attempt_role"] != LiveAttemptRoleV1.HISTORY_POLICY.value
        or proof["attempt_status"] != receipt.status.value
        or type(proof["attempt_dispatch_count"]) is not int
        or proof["attempt_dispatch_count"] != receipt.dispatch_count
        or proof["attempt_receipt_sha256"] != live_attempt_receipt_sha256(receipt)
        or proof["attempt_authority_sha256"] != live_attempt_authority_sha256(authority)
        or proof["provider_request_sha256"] != request.request_sha256
        or proof["provider_output_schema_sha256"] != history_policy_transport_schema_v1().sha256
        or proof["r22_output_schema_sha256"] != ProposalSchemaSnapshotV1.from_checked_in().sha256
        or type(proof["provider_request_byte_count"]) is not int
        or proof["provider_request_byte_count"] != request.byte_count
        or proof["r22_policy_receipt_sha256"] != (None if r22 is None else r22.sha256)
    ):
        raise R24ContractError(
            "INVALID_HISTORY_REQUEST_PROOF",
            "durable history proof hashes or terminal state differ",
        )
    expected_bindings = (
        (
            "attempt authority",
            expected_attempt_authority_sha256,
            live_attempt_authority_sha256(authority),
        ),
        (
            "attempt constraint",
            expected_constraint_binding_sha256,
            canonical_sha256(
                cast(
                    JsonValue,
                    live_rubric_attempt_constraint_binding_projection(constraint),
                )
            ),
        ),
        ("manifest", expected_manifest_sha256, authority.manifest_sha256),
        ("preflight", expected_preflight_sha256, authority.preflight_sha256),
        (
            "case execution lease",
            expected_case_execution_lease_sha256,
            authority.case_execution_lease_sha256,
        ),
        ("stage", expected_stage_sha256, authority.stage_sha256),
        (
            "pricing",
            expected_pricing_binding_sha256,
            authority.pricing_binding_sha256,
        ),
        (
            "transport",
            expected_transport_binding_sha256,
            authority.transport_binding_sha256,
        ),
        ("request", expected_request_sha256, authority.request_sha256),
    )
    for label, expected, observed in expected_bindings:
        try:
            trusted_expected = _require_sha256(expected, f"expected {label} SHA-256")
        except Exception as exc:
            raise R24ContractError(
                "INVALID_HISTORY_REQUEST_PROOF",
                f"caller-known expected {label} binding is invalid",
            ) from exc
        if trusted_expected != observed:
            raise R24ContractError(
                "INVALID_HISTORY_REQUEST_PROOF",
                f"history proof differs from caller-known expected {label} binding",
            )
    validate_live_history_policy_attempt_request_anchor_v1(anchor, attempt_receipt=receipt)


def validate_live_rubric_cross_bindings_v1(
    *,
    logical_call_id: str,
    actor_request_sha256: str,
    attempts: tuple[LiveAttemptReceiptV1, ...],
    rubric_attempt_request_anchors: tuple[LiveRubricAttemptRequestAnchorV1, ...],
    rubric_call_receipts: tuple[LiveRubricCallReceiptV1, ...],
    rubric_call_trust_anchors: tuple[LiveRubricCallTrustAnchorV1, ...],
    expected_collector_stimulus_sha256: str | None,
    expected_tracking_packet_sha256: str | None,
    rubric_backend_extension: R24RubricBackendExtensionDescriptorV1 | None,
    binding: ResolvedLivePolicyCallBindingV1 | None,
    actor_call_index: int | None,
    expect_history_policy: bool | None,
    allow_incomplete: bool,
    history_policy_attempt_request_anchor: (LiveHistoryPolicyAttemptRequestAnchorV1 | None) = None,
) -> None:
    """Validate one exact ordered R2.4 rubric/attempt/binding proof graph.

    ``allow_incomplete`` is reserved for the generic Original-fallback path,
    where a terminal failed attempt can exist without a completed rubric call
    receipt or resolved policy binding.  Every completed rubric call receipt
    must still match the corresponding ordered RUBRIC attempt exactly.
    """

    _require_id(logical_call_id, "logical_call_id")
    _require_sha256(actor_request_sha256, "actor_request_sha256")
    if expected_collector_stimulus_sha256 is not None:
        _require_sha256(
            expected_collector_stimulus_sha256,
            "expected_collector_stimulus_sha256",
        )
    if expected_tracking_packet_sha256 is not None:
        _require_sha256(
            expected_tracking_packet_sha256,
            "expected_tracking_packet_sha256",
        )
    if type(allow_incomplete) is not bool:
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "incomplete-proof flag is untrusted"
        )
    if actor_call_index is not None and (
        type(actor_call_index) is not int or actor_call_index <= 0
    ):
        raise R24ContractError("RUBRIC_CROSS_BINDING_MISMATCH", "actor call index is invalid")
    if expect_history_policy is not None and type(expect_history_policy) is not bool:
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "history-policy expectation is untrusted"
        )
    if (
        type(attempts) is not tuple
        or type(rubric_attempt_request_anchors) is not tuple
        or type(rubric_call_receipts) is not tuple
        or type(rubric_call_trust_anchors) is not tuple
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "rubric proof collections are not tuples"
        )
    if any(type(item) is not LiveAttemptReceiptV1 for item in attempts) or any(
        type(item) is not LiveRubricCallReceiptV1 for item in rubric_call_receipts
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "rubric proof values have untrusted types"
        )
    if any(
        type(item) is not LiveRubricAttemptRequestAnchorV1
        for item in rubric_attempt_request_anchors
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "rubric attempt request anchors have untrusted types",
        )
    if any(type(item) is not LiveRubricCallTrustAnchorV1 for item in rubric_call_trust_anchors):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "rubric trust anchors have untrusted types"
        )

    try:
        trusted_attempts = tuple(snapshot_live_attempt_receipt(item) for item in attempts)
        trusted_attempt_anchors = tuple(
            snapshot_live_rubric_attempt_request_anchor(item)
            for item in rubric_attempt_request_anchors
        )
        trusted_rubric_calls = tuple(
            snapshot_live_rubric_call_receipt(item) for item in rubric_call_receipts
        )
        trusted_rubric_anchors = tuple(
            snapshot_live_rubric_call_trust_anchor(item) for item in rubric_call_trust_anchors
        )
        trusted_extension = (
            None
            if rubric_backend_extension is None
            else snapshot_r24_rubric_backend_extension_descriptor(rubric_backend_extension)
        )
        trusted_binding = (
            None
            if binding is None
            else ResolvedLivePolicyCallBindingV1(
                **{name: getattr(binding, name) for name in binding.__dataclass_fields__}
            )
        )
        trusted_history_anchor = (
            None
            if history_policy_attempt_request_anchor is None
            else snapshot_live_history_policy_attempt_request_anchor(
                history_policy_attempt_request_anchor
            )
        )
    except Exception as exc:
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "rubric proof snapshot failed"
        ) from exc

    if (
        len({item.attempt_id for item in trusted_attempts}) != len(trusted_attempts)
        or len({item.attempt_id for item in trusted_attempt_anchors})
        != len(trusted_attempt_anchors)
        or len({item.receipt_id for item in trusted_rubric_calls}) != len(trusted_rubric_calls)
    ):
        raise R24ContractError("RUBRIC_CROSS_BINDING_MISMATCH", "rubric proof repeats an identity")
    if (
        any(
            item.logical_call_id != logical_call_id
            or item.actor_request_sha256 != actor_request_sha256
            for item in trusted_attempts
        )
        or any(item.logical_call_id != logical_call_id for item in trusted_attempt_anchors)
        or any(item.logical_call_id != logical_call_id for item in trusted_rubric_calls)
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "rubric proof belongs to another actor call"
        )

    if trusted_extension is None:
        if (
            trusted_attempts
            or trusted_attempt_anchors
            or trusted_rubric_calls
            or trusted_rubric_anchors
            or expected_collector_stimulus_sha256 is not None
            or expected_tracking_packet_sha256 is not None
            or trusted_binding is not None
            or actor_call_index is not None
            or trusted_history_anchor is not None
        ):
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "nonempty rubric proof lacks its extension descriptor",
            )
        if not allow_incomplete:
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH", "complete rubric proof is absent"
            )
        return
    if trusted_extension.execution_scope is not LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE:
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "production rubric proof has a non-live extension scope",
        )
    if trusted_extension.prompt_sha256 != live_rubric_prompt_bundle_sha256():
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "rubric extension does not bind the module-owned prompt bytes",
        )
    if (
        trusted_rubric_anchors or trusted_attempt_anchors
    ) and expected_collector_stimulus_sha256 is None:
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "rubric trust anchors lack their coordinator-owned Collector root",
        )
    if (
        any(item.operation is LiveRubricOperationV1.TRACK for item in trusted_attempt_anchors)
        and expected_tracking_packet_sha256 is None
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "tracking trust anchor lacks its coordinator-owned packet root",
        )

    rubric_attempts = tuple(
        item for item in trusted_attempts if item.role is LiveAttemptRoleV1.RUBRIC
    )
    history_attempts = tuple(
        item for item in trusted_attempts if item.role is LiveAttemptRoleV1.HISTORY_POLICY
    )
    if trusted_attempt_anchors:
        constraint = trusted_attempt_anchors[0].constraint_binding
        if (
            any(item.constraint_binding != constraint for item in trusted_attempt_anchors[1:])
            or len(trusted_attempts) > constraint.max_openai_calls
            or (actor_call_index is not None and actor_call_index > constraint.max_actor_calls)
        ):
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "attempt census differs from the sealed case constraint",
            )
    if len(history_attempts) > 1 or (
        bool(history_attempts) != (trusted_history_anchor is not None)
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "history attempt/request-proof census differs",
        )
    if trusted_history_anchor is not None:
        try:
            validate_live_history_policy_attempt_request_anchor_v1(
                trusted_history_anchor,
                attempt_receipt=history_attempts[0],
            )
        except Exception as exc:
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "history attempt differs from its complete request proof",
            ) from exc
        if trusted_attempt_anchors and (
            trusted_history_anchor.constraint_binding
            != trusted_attempt_anchors[0].constraint_binding
        ):
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "history and rubric attempts use different case constraints",
            )
    proof_complete = not allow_incomplete or trusted_binding is not None
    if trusted_attempts and (actor_call_index is None or expect_history_policy is None):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "rubric attempt sequence authority is absent"
        )
    if trusted_binding is not None and actor_call_index != trusted_binding.actor_call_index:
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "actor call index differs from the binding"
        )
    if (
        len(trusted_rubric_anchors) != len(trusted_rubric_calls)
        or len(trusted_rubric_calls) > len(rubric_attempts)
        or len(trusted_attempt_anchors) < len(trusted_rubric_calls)
        or any(
            item.status is LiveAttemptStatusV1.COMPLETED
            for item in rubric_attempts[len(trusted_rubric_calls) :]
        )
        or (proof_complete and len(trusted_rubric_calls) != len(rubric_attempts))
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "rubric call/attempt census differs"
        )

    operations = tuple(item.operation for item in trusted_rubric_calls)
    expected_rubric_operations = (
        (LiveRubricOperationV1.GENERATE, LiveRubricOperationV1.TRACK)
        if actor_call_index == 1
        else (LiveRubricOperationV1.TRACK,)
    )
    expected_roles = (
        (LiveAttemptRoleV1.RUBRIC,) * len(expected_rubric_operations)
        + ((LiveAttemptRoleV1.HISTORY_POLICY,) if expect_history_policy else ())
        if actor_call_index is not None and expect_history_policy is not None
        else ()
    )
    actual_roles = tuple(item.role for item in trusted_attempts)
    if (
        operations != expected_rubric_operations[: len(operations)]
        or actual_roles != expected_roles[: len(actual_roles)]
        or (proof_complete and operations != expected_rubric_operations)
        or (proof_complete and actual_roles != expected_roles)
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH", "rubric operation or attempt order differs"
        )
    if (
        len(trusted_attempt_anchors) > len(rubric_attempts)
        or tuple(item.attempt_order for item in trusted_attempt_anchors)
        != tuple(range(1, len(trusted_attempt_anchors) + 1))
        or (proof_complete and len(trusted_attempt_anchors) != len(rubric_attempts))
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "rubric request-anchor/attempt census or order differs",
        )
    for index, (request_anchor, attempt) in enumerate(
        zip(
            trusted_attempt_anchors,
            rubric_attempts[: len(trusted_attempt_anchors)],
            strict=True,
        )
    ):
        try:
            anchored_request_sha256 = validate_live_rubric_attempt_request_anchor_v1(
                request_anchor,
                backend_extension=trusted_extension,
                attempt_receipt=attempt,
            )
            validate_live_rubric_attempt_receipt_authority_v1(
                request_anchor,
                attempt,
            )
        except Exception as exc:
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "rubric attempt request does not match its sealed anchor",
            ) from exc
        anchored_packet_sha256: str | None = None
        if request_anchor.operation is LiveRubricOperationV1.TRACK:
            packet = request_anchor.provider_input.get("packet")
            if type(packet) is not dict:
                raise R24ContractError(
                    "RUBRIC_CROSS_BINDING_MISMATCH",
                    "tracking attempt anchor omitted its exact packet",
                )
            anchored_packet_sha256 = canonical_sha256(cast(JsonValue, packet))
        if (
            request_anchor.operation is not expected_rubric_operations[index]
            or request_anchor.logical_call_id != logical_call_id
            or request_anchor.attempt_id != attempt.attempt_id
            or request_anchor.attempt_order != index + 1
            or attempt.request_sha256 != anchored_request_sha256
            or rubric_evidence_snapshot_sha256(request_anchor.collector_stimulus)
            != expected_collector_stimulus_sha256
            or anchored_packet_sha256
            != (
                None
                if request_anchor.operation is LiveRubricOperationV1.GENERATE
                else expected_tracking_packet_sha256
            )
        ):
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "rubric attempt differs from its request preimage or coordinator root",
            )
    unanchored_attempts = rubric_attempts[len(trusted_attempt_anchors) :]
    if unanchored_attempts:
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "formed rubric attempt lacks its pre-transport request anchor",
        )
    if not proof_complete and len(trusted_rubric_calls) < len(rubric_attempts):
        first_unmatched = tuple(
            index
            for index, item in enumerate(trusted_attempts)
            if item.role is LiveAttemptRoleV1.RUBRIC
        )[len(trusted_rubric_calls)]
        if first_unmatched != len(trusted_attempts) - 1:
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "an unmatched rubric attempt is followed by another attempt",
            )

    for rubric_call, attempt, request_anchor, trust_anchor in zip(
        trusted_rubric_calls,
        rubric_attempts[: len(trusted_rubric_calls)],
        trusted_attempt_anchors[: len(trusted_rubric_calls)],
        trusted_rubric_anchors,
        strict=True,
    ):
        generate = rubric_call.operation is LiveRubricOperationV1.GENERATE
        expected_input_schema = (
            LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION
            if generate
            else LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION
        )
        expected_output_schema = (
            trusted_extension.generate_output_schema_sha256
            if generate
            else trusted_extension.track_output_schema_sha256
        )
        expected_prompt_sha256 = live_rubric_operation_prompt_sha256(rubric_call.operation)
        envelope = trust_anchor.response_envelope
        response_envelope_sha256 = canonical_sha256(
            cast(JsonValue, responses_envelope_hash_projection(envelope))
        )
        stimulus_sha256 = rubric_evidence_snapshot_sha256(trust_anchor.collector_stimulus)
        anchored_tracking_packet_sha256: str | None = None
        if not generate:
            provider_input = trust_anchor.provider_input
            packet = provider_input.get("packet")
            if type(packet) is not dict:
                raise R24ContractError(
                    "RUBRIC_CROSS_BINDING_MISMATCH",
                    "tracking trust anchor omitted its exact packet",
                )
            anchored_tracking_packet_sha256 = canonical_sha256(cast(JsonValue, packet))
        try:
            anchored_provider_request_sha256 = validate_live_rubric_request_anchor_v1(
                trust_anchor,
                backend_extension=trusted_extension,
            )
        except Exception as exc:
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "rubric provider request does not match its trust anchor",
            ) from exc
        image = trust_anchor.current_image
        image_binding_sha256: str | None = None
        if not generate:
            if type(image) is not BoundCollectorCurrentImageV1:
                raise R24ContractError(
                    "RUBRIC_CROSS_BINDING_MISMATCH",
                    "tracking trust anchor omitted the current image",
                )
            try:
                rebound_image = bind_current_collector_image_projection(
                    stimulus=trust_anchor.collector_stimulus,
                    current_image_data_url=image.data_url,
                    current_image_sha256=image.content_sha256,
                    logical_call_id=logical_call_id,
                )
            except Exception as exc:
                raise R24ContractError(
                    "RUBRIC_CROSS_BINDING_MISMATCH",
                    "tracking image does not bind to the exact Collector context",
                ) from exc
            if rebound_image != image or image.stimulus_sha256 != stimulus_sha256:
                raise R24ContractError(
                    "RUBRIC_CROSS_BINDING_MISMATCH",
                    "tracking image differs from its recomputed Collector binding",
                )
            image_binding_sha256 = rebound_image.binding_sha256
        if (
            attempt.role is not LiveAttemptRoleV1.RUBRIC
            or attempt.status is not LiveAttemptStatusV1.COMPLETED
            or not attempt.passed
            or rubric_call.backend_extension_descriptor_sha256 != trusted_extension.sha256
            or rubric_call.r23_compatibility_descriptor_sha256
            != trusted_extension.r23_compatibility_descriptor_sha256
            or rubric_call.execution_scope is not trusted_extension.execution_scope
            or rubric_call.transport_kind is not trusted_extension.transport_kind
            or rubric_call.transport_authority is not trusted_extension.transport_authority
            or rubric_call.requested_model != trusted_extension.configured_model
            or rubric_call.returned_model != trusted_extension.configured_model
            or rubric_call.requested_model != attempt.requested_model
            or rubric_call.returned_model != attempt.returned_model
            or rubric_call.provider_input_schema_version != expected_input_schema
            or rubric_call.provider_output_schema_sha256 != expected_output_schema
            or rubric_call.prompt_sha256 != expected_prompt_sha256
            or trust_anchor.operation is not rubric_call.operation
            or request_anchor.operation is not rubric_call.operation
            or request_anchor.attempt_id != attempt.attempt_id
            or trust_anchor.task_run_id != rubric_call.task_run_id
            or request_anchor.task_run_id != trust_anchor.task_run_id
            or trust_anchor.logical_call_id != logical_call_id
            or rubric_evidence_snapshot_sha256(request_anchor.collector_stimulus) != stimulus_sha256
            or request_anchor.current_image != image
            or canonical_json_bytes(cast(JsonValue, request_anchor.provider_input))
            != canonical_json_bytes(cast(JsonValue, trust_anchor.provider_input))
            or request_anchor.provider_request != trust_anchor.provider_request
            or stimulus_sha256 != expected_collector_stimulus_sha256
            or anchored_tracking_packet_sha256
            != (None if generate else expected_tracking_packet_sha256)
            or (image is None) != generate
            or rubric_call.current_image_binding_sha256 != image_binding_sha256
            or envelope.sha256 != response_envelope_sha256
            or attempt.response_envelope_sha256 != response_envelope_sha256
            or rubric_call.provider_output_sha256 != envelope.output_text_sha256
            or rubric_call.requested_model != envelope.requested_model
            or rubric_call.returned_model != envelope.returned_model
            or rubric_call.input_tokens != envelope.input_tokens
            or rubric_call.output_tokens != envelope.output_tokens
            or rubric_call.total_tokens != envelope.total_tokens
            or rubric_call.manifest_sha256 != attempt.manifest_sha256
            or rubric_call.preflight_sha256 != attempt.preflight_sha256
            or rubric_call.case_execution_lease_sha256 != attempt.case_execution_lease_sha256
            or rubric_call.stage_sha256 != attempt.stage_sha256
            or rubric_call.attempt_authority_sha256 != attempt.authority_sha256
            or rubric_call.attempt_receipt_sha256 != live_attempt_receipt_sha256(attempt)
            or rubric_call.provider_request_sha256 != anchored_provider_request_sha256
            or attempt.request_sha256 != anchored_provider_request_sha256
            or rubric_call.transport_binding_sha256 != attempt.transport_binding_sha256
            or rubric_call.pricing_binding_sha256 != attempt.pricing_binding_sha256
            or rubric_call.dispatch_count != attempt.dispatch_count
            or rubric_call.input_tokens != attempt.input_tokens
            or rubric_call.output_tokens != attempt.output_tokens
            or rubric_call.total_tokens != attempt.total_tokens
            or rubric_call.cost_usd_micros != attempt.cost_usd_micros
        ):
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH",
                "R2.4 rubric call receipt differs from its extension or attempt",
            )

    if trusted_binding is None:
        if not allow_incomplete:
            raise R24ContractError(
                "RUBRIC_CROSS_BINDING_MISMATCH", "complete policy binding is absent"
            )
        return

    rubric_attempt_hashes = tuple(live_attempt_receipt_sha256(item) for item in rubric_attempts)
    rubric_call_hashes = tuple(
        live_rubric_call_receipt_sha256(item) for item in trusted_rubric_calls
    )
    history_attempt_hashes = tuple(
        live_attempt_receipt_sha256(item)
        for item in trusted_attempts
        if item.role is LiveAttemptRoleV1.HISTORY_POLICY
    )
    expected_history_hashes = (
        ()
        if trusted_binding.history_policy_attempt_receipt_sha256 is None
        else (trusted_binding.history_policy_attempt_receipt_sha256,)
    )
    exact_costs = tuple(item.cost_usd_micros for item in trusted_attempts)
    if (
        any(not item.passed for item in trusted_attempts)
        or trusted_binding.logical_call_id != logical_call_id
        or trusted_binding.actor_request_sha256 != actor_request_sha256
        or trusted_binding.rubric_backend_extension_descriptor_sha256 != trusted_extension.sha256
        or any(
            item.manifest_sha256 != trusted_binding.execution_authority_sha256
            or item.case_execution_lease_sha256 != trusted_binding.case_execution_lease_sha256
            or item.preflight_sha256 != trusted_binding.preflight_report_sha256
            or item.pricing_binding_sha256 != trusted_binding.pricing_binding_sha256
            for item in trusted_attempts
        )
        or rubric_attempt_hashes != trusted_binding.rubric_attempt_receipt_sha256s
        or rubric_call_hashes != trusted_binding.rubric_call_receipt_sha256s
        or history_attempt_hashes != expected_history_hashes
        or (
            bool(history_attempt_hashes)
            and trusted_attempts[-1].transport_binding_sha256
            != trusted_binding.source_transport_binding_sha256
        )
        or trusted_binding.openai_calls != len(trusted_attempts)
        or trusted_binding.openai_calls != sum(item.dispatch_count for item in trusted_attempts)
        or any(value is None for value in exact_costs)
        or trusted_binding.cost_usd_micros != sum(cast(int, value) for value in exact_costs)
    ):
        raise R24ContractError(
            "RUBRIC_CROSS_BINDING_MISMATCH",
            "live policy binding differs from its ordered attempt proof",
        )


class _PerCallAdmissionBridgeV1:
    """Module-owned stateful bridge for one GPT policy evaluation."""

    def __init__(self, coordinator: R24RuntimeCoordinatorV1, *, seal: object) -> None:
        if seal is not _PER_CALL_POLICY_SEAL or type(coordinator) is not R24RuntimeCoordinatorV1:
            raise PermissionError("per-call admission bridge is module-owned")
        self._coordinator = coordinator
        self._packet: EvidencePacketV1 | None = None
        self._request: JsonValue | None = None
        self._history_ir: HistoryIR | None = None

    @property
    def evidence_packet_sha256(self) -> str | None:
        packet = self._packet
        return None if packet is None else evidence_packet_sha256(deepcopy(packet))

    def evidence(
        self,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> Any:
        evidence = self._coordinator(request, context, history_ir)
        self._packet = deepcopy(evidence.packet)
        self._request = copy_json(request)
        self._history_ir = deepcopy(history_ir)
        return evidence

    def admit(
        self,
        packet_projection: dict[str, JsonValue],
        proposal_projection: dict[str, JsonValue],
        provenance: PolicyCallProvenanceV1,
    ) -> RuntimeAdmissionBundleV1:
        del packet_projection
        packet = self._packet
        request = self._request
        history_ir = self._history_ir
        if packet is None or request is None or history_ir is None:
            raise R24ContractError(
                "PER_CALL_EVIDENCE_MISSING", "admission ran before exact evidence construction"
            )
        return proposal_admission(
            deepcopy(packet),
            proposal_projection,
            provenance,
            source_request=copy_json(request),
            history_ir=deepcopy(history_ir),
        )


def _manifest_case_descriptor(
    manifest: R24R25RunAuthorityManifestV1 | R24SmokeRunAuthorityManifestV1,
    *,
    stage: RunStageV1,
    host: PilotHostV1,
    mode: SmokeModeV1,
    case_id: str,
) -> OwnerAuthorizedLiveCaseDescriptorV1:
    if stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}:
        expected_stage = (
            RunStageV1.QWEN_LIVE_SMOKE
            if host is PilotHostV1.QWEN3_VL
            else RunStageV1.MAI_LIVE_SMOKE
        )
        plan = next((value for value in manifest.smoke_plans if value.host is host), None)
        case = next(
            (
                value
                for value in (() if plan is None else plan.cases)
                if value.case_id == case_id and value.mode is mode
            ),
            None,
        )
        if stage is not expected_stage or case is None:
            raise R24ContractError(
                "CASE_OUTSIDE_MANIFEST", "smoke case differs from owner authority"
            )
        return OwnerAuthorizedLiveCaseDescriptorV1(
            stage=stage,
            host=host,
            mode=mode,
            case_id=case.case_id,
            task_id=case.task_id,
            task_parameters_sha256=None,
            reset_seed=None,
            max_actor_calls=case.max_actor_calls,
            max_openai_calls=case.max_openai_calls,
            max_wall_time_seconds=case.max_wall_time_seconds,
            max_cost_usd_micros=case.max_cost_usd_micros,
        )
    if stage is not RunStageV1.R25_PILOT or mode is SmokeModeV1.SHADOW:
        raise R24ContractError("CASE_OUTSIDE_MANIFEST", "pilot case stage or mode differs")
    if type(manifest) is R24SmokeRunAuthorityManifestV1:
        raise R24ContractError("CASE_OUTSIDE_MANIFEST", "smoke authority has no pilot cases")
    joint_manifest = cast(R24R25RunAuthorityManifestV1, manifest)
    prefix = "pilot-cell-"
    index_text = case_id.removeprefix(prefix)
    if not case_id.startswith(prefix) or len(index_text) != 3 or not index_text.isdigit():
        raise R24ContractError("CASE_OUTSIDE_MANIFEST", "pilot case ID is not canonical")
    try:
        cell = joint_manifest.pilot.cells[int(index_text)]
    except (IndexError, ValueError) as exc:
        raise R24ContractError("CASE_OUTSIDE_MANIFEST", "pilot case is absent") from exc
    expected_mode = SmokeModeV1.OFF if cell.arm is PilotArmV1.BASELINE else SmokeModeV1.ACTIVE
    if cell.host is not host or mode is not expected_mode:
        raise R24ContractError("CASE_OUTSIDE_MANIFEST", "pilot cell binding differs")
    possible_openai_calls = 2 * joint_manifest.pilot.max_steps_per_cell + 1
    return OwnerAuthorizedLiveCaseDescriptorV1(
        stage=stage,
        host=host,
        mode=mode,
        case_id=case_id,
        task_id=cell.task_id,
        task_parameters_sha256=cell.task_parameters_sha256,
        reset_seed=cell.reset_seed,
        max_actor_calls=joint_manifest.pilot.max_steps_per_cell,
        max_openai_calls=min(possible_openai_calls, joint_manifest.pilot.max_total_openai_calls),
        max_wall_time_seconds=joint_manifest.pilot.per_cell_timeout_seconds,
        max_cost_usd_micros=joint_manifest.pilot.max_total_cost_usd_micros,
    )


@dataclass(frozen=True, slots=True)
class _LiveBudgetReservationV1:
    reservation_id: str
    case_key: str
    reserved_cost_usd_micros: int


class ProductionLiveBudgetLedgerV1:
    """Run-owned atomic budget grants for all live Sentinel cases.

    Pilot authority is partitioned once across the owner-pinned JOINT cells;
    no independently constructed cell policy can inherit the whole-run cost
    ceiling.  Each actor call reserves its complete worst-case attempt grant
    before a provider child may be dispatched.  Failed/unknown calls retain
    the reservation and close the ledger fail-closed.
    """

    __slots__ = (
        "_case_grants",
        "_case_reserved",
        "_case_spent",
        "_factory_binding_sha256",
        "_failed",
        "_manifest_sha256",
        "_reservations",
        "_lock",
    )

    def __init__(
        self,
        factory: ProductionPostPreflightFactoryV1,
        *,
        seal: object,
    ) -> None:
        if (
            seal is not _LIVE_BUDGET_LEDGER_SEAL
            or type(factory) is not ProductionPostPreflightFactoryV1
        ):
            raise PermissionError("production live budget ledger is module-owned")
        manifest = factory.manifest_snapshot()
        case_grants: dict[str, int] = {}
        if type(manifest) is R24R25RunAuthorityManifestV1:
            joint_cells = tuple(
                (index, cell)
                for index, cell in enumerate(manifest.pilot.cells)
                if cell.arm is PilotArmV1.JOINT_SENTINEL
            )
            if not joint_cells:
                raise R24ContractError("INVALID_PILOT_BUDGET", "pilot has no live cells")
            total = manifest.pilot.max_total_cost_usd_micros
            base, remainder = divmod(total, len(joint_cells))
            for ordinal, (index, _) in enumerate(joint_cells):
                case_grants[f"{RunStageV1.R25_PILOT.value}:pilot-cell-{index:03d}"] = base + (
                    1 if ordinal < remainder else 0
                )
        elif type(manifest) is not R24SmokeRunAuthorityManifestV1:
            raise R24ContractError("UNTRUSTED_LIVE_AUTHORITY", "budget manifest type differs")
        for plan in manifest.smoke_plans:
            stage = (
                RunStageV1.QWEN_LIVE_SMOKE
                if plan.host is PilotHostV1.QWEN3_VL
                else RunStageV1.MAI_LIVE_SMOKE
            )
            for case in plan.cases:
                if case.mode is not SmokeModeV1.OFF:
                    case_grants[f"{stage.value}:{case.case_id}"] = case.max_cost_usd_micros
        if any(value <= 0 for value in case_grants.values()):
            raise R24ContractError(
                "INVALID_LIVE_BUDGET", "every live case needs a positive fixed cost grant"
            )
        self._manifest_sha256 = factory.manifest_sha256
        self._factory_binding_sha256 = factory.factory_binding_sha256
        self._case_grants = case_grants
        self._case_reserved = {key: 0 for key in case_grants}
        self._case_spent = {key: 0 for key in case_grants}
        self._reservations: dict[str, _LiveBudgetReservationV1] = {}
        self._failed = False
        self._lock = Lock()

    def _case_key(self, descriptor: OwnerAuthorizedLiveCaseDescriptorV1) -> str:
        return f"{descriptor.stage.value}:{descriptor.case_id}"

    def grant_descriptor(
        self,
        factory: ProductionPostPreflightFactoryV1,
        descriptor: OwnerAuthorizedLiveCaseDescriptorV1,
    ) -> OwnerAuthorizedLiveCaseDescriptorV1:
        if (
            type(factory) is not ProductionPostPreflightFactoryV1
            or factory.manifest_sha256 != self._manifest_sha256
            or factory.factory_binding_sha256 != self._factory_binding_sha256
            or type(descriptor) is not OwnerAuthorizedLiveCaseDescriptorV1
        ):
            raise R24ContractError("LIVE_BUDGET_AUTHORITY_MISMATCH", "budget authority differs")
        key = self._case_key(descriptor)
        with self._lock:
            grant = self._case_grants.get(key)
            if grant is None or self._failed:
                raise R24ContractError("LIVE_BUDGET_UNAVAILABLE", "case cost grant is unavailable")
        return replace(descriptor, max_cost_usd_micros=grant)

    def attempt_ceiling(self, descriptor: OwnerAuthorizedLiveCaseDescriptorV1) -> int:
        key = self._case_key(descriptor)
        with self._lock:
            grant = self._case_grants.get(key)
            if grant != descriptor.max_cost_usd_micros or grant is None:
                raise R24ContractError("LIVE_BUDGET_AUTHORITY_MISMATCH", "case grant differs")
        ceiling = grant // descriptor.max_openai_calls
        if ceiling <= 0:
            raise R24ContractError("LIVE_BUDGET_TOO_SMALL", "attempt cost grant is zero")
        return ceiling

    def reserve_call(
        self,
        descriptor: OwnerAuthorizedLiveCaseDescriptorV1,
        *,
        logical_call_id: str,
        actor_call_index: int,
        attempt_count: int,
    ) -> _LiveBudgetReservationV1:
        _require_id(logical_call_id, "logical_call_id")
        if type(attempt_count) is not int or not 1 <= attempt_count <= 3:
            raise R24ContractError("INVALID_LIVE_BUDGET", "attempt reservation count differs")
        ceiling = self.attempt_ceiling(descriptor)
        amount = ceiling * attempt_count
        key = self._case_key(descriptor)
        reservation_id = canonical_sha256(
            cast(
                JsonValue,
                {
                    "actor_call_index": actor_call_index,
                    "case_key": key,
                    "factory_binding_sha256": self._factory_binding_sha256,
                    "logical_call_id": logical_call_id,
                    "reserved_cost_usd_micros": amount,
                },
            )
        )
        with self._lock:
            if self._failed or reservation_id in self._reservations:
                raise R24ContractError("LIVE_BUDGET_UNAVAILABLE", "budget reservation unavailable")
            grant = self._case_grants[key]
            if self._case_spent[key] + self._case_reserved[key] + amount > grant:
                raise R24ContractError("LIVE_CASE_COST_BUDGET_EXCEEDED", "case budget exhausted")
            reservation = _LiveBudgetReservationV1(reservation_id, key, amount)
            self._reservations[reservation_id] = reservation
            self._case_reserved[key] += amount
            return reservation

    def settle_call(
        self,
        reservation: _LiveBudgetReservationV1,
        *,
        exact_cost_usd_micros: int,
    ) -> None:
        if type(reservation) is not _LiveBudgetReservationV1 or (
            type(exact_cost_usd_micros) is not int or exact_cost_usd_micros < 0
        ):
            raise R24ContractError("INVALID_LIVE_BUDGET", "budget settlement differs")
        with self._lock:
            current = self._reservations.pop(reservation.reservation_id, None)
            if (
                current != reservation
                or exact_cost_usd_micros > reservation.reserved_cost_usd_micros
            ):
                self._failed = True
                raise R24ContractError(
                    "LIVE_COST_RESERVATION_EXCEEDED", "terminal cost exceeds reserve"
                )
            self._case_reserved[reservation.case_key] -= reservation.reserved_cost_usd_micros
            self._case_spent[reservation.case_key] += exact_cost_usd_micros

    def freeze_failed_call(self, reservation: _LiveBudgetReservationV1) -> None:
        if type(reservation) is not _LiveBudgetReservationV1:
            raise R24ContractError("INVALID_LIVE_BUDGET", "failed reservation differs")
        with self._lock:
            if self._reservations.get(reservation.reservation_id) != reservation:
                raise R24ContractError("INVALID_LIVE_BUDGET", "failed reservation is absent")
            self._failed = True


def build_production_live_budget_ledger_v1(
    factory: ProductionPostPreflightFactoryV1,
) -> ProductionLiveBudgetLedgerV1:
    """Create the sole run-shared live cost authority from a sealed factory."""

    return ProductionLiveBudgetLedgerV1(factory, seal=_LIVE_BUDGET_LEDGER_SEAL)


class OwnerAuthorizedLivePerCallPolicyV1:
    """Resolve one request-bound live policy only after the raw actor request exists.

    The object owns no secret and accepts no transport, client, callback, or command.
    Exact attempt runners keep secret acquisition inside their child process.  One
    instance is bound to one smoke case or pilot cell and serializes its actor calls,
    which also preserves the per-task live-rubric state machine.
    """

    execution_scope = RuntimeVerticalExecutionScope.OWNER_AUTHORIZED_LIVE_ACTIVE

    def __init__(
        self,
        *,
        factory: ProductionPostPreflightFactoryV1,
        pricing: LiveAttemptPricingV1,
        budget_ledger: ProductionLiveBudgetLedgerV1,
        stage: RunStageV1,
        host: PilotHostV1,
        mode: SmokeModeV1,
        case_id: str,
        case_deadline_monotonic_ns: int,
        seal: object,
    ) -> None:
        if seal is not _PER_CALL_POLICY_SEAL:
            raise PermissionError("per-call production policy is module-owned")
        if type(factory) is not ProductionPostPreflightFactoryV1:
            raise R24ContractError(
                "POST_PREFLIGHT_FACTORY_REQUIRED", "exact production factory is required"
            )
        if type(pricing) is not LiveAttemptPricingV1:
            raise R24ContractError("PRICING_REQUIRED", "exact live pricing is required")
        if type(budget_ledger) is not ProductionLiveBudgetLedgerV1:
            raise R24ContractError("LIVE_BUDGET_REQUIRED", "exact shared budget ledger is required")
        pricing_sha256 = live_attempt_pricing_sha256(pricing)
        if pricing_sha256 != factory.pricing_binding_sha256:
            raise R24ContractError("PRICING_AUTHORITY_MISMATCH", "pricing pin differs")
        manifest = factory.manifest_snapshot()
        manifest_sha256 = _live_manifest_sha256(manifest)
        if manifest_sha256 != factory.manifest_sha256:
            raise R24ContractError("FACTORY_MANIFEST_DRIFT", "factory manifest snapshot differs")
        descriptor = budget_ledger.grant_descriptor(
            factory,
            _manifest_case_descriptor(
                manifest,
                stage=stage,
                host=host,
                mode=mode,
                case_id=case_id,
            ),
        )
        now_ns = monotonic_ns()
        if (
            type(case_deadline_monotonic_ns) is not int
            or case_deadline_monotonic_ns <= now_ns
            or case_deadline_monotonic_ns
            > now_ns + descriptor.max_wall_time_seconds * 1_000_000_000
        ):
            raise R24ContractError("INVALID_CASE_DEADLINE", "case deadline exceeds authority")
        authority = issue_owner_authorized_live_policy_authority(
            manifest,
            confirmed_manifest_sha256=factory.manifest_sha256,
        )
        history_stage = factory.openai_stage(OpenAIRoleV1.HISTORY_POLICY)
        live_descriptor = TransportDescriptorV1(
            transport_kind=history_stage.transport_kind,
            transport_authority=history_stage.transport_authority,
            openai_sdk_version=history_stage.openai_sdk_version,
            sdk_max_retries=history_stage.sdk_max_retries,
            external_network_on_call=history_stage.external_network_on_call,
            model_on_call=history_stage.model_on_call,
        )
        _require_descriptor_matches_stage(live_descriptor, history_stage)
        policy_seed = canonical_sha256(
            cast(
                JsonValue,
                {
                    "case_id": descriptor.case_id,
                    "case_cost_grant_usd_micros": descriptor.max_cost_usd_micros,
                    "factory_binding_sha256": factory.factory_binding_sha256,
                    "host": descriptor.host.value,
                    "manifest_sha256": factory.manifest_sha256,
                    "mode": descriptor.mode.value,
                    "pricing_binding_sha256": pricing_sha256,
                    "stage": descriptor.stage.value,
                },
            )
        )
        self._policy_id = f"r24-live-per-call-{policy_seed[:32]}"
        self._factory = factory
        self._pricing = pricing
        self._budget_ledger = budget_ledger
        self._authority = authority
        self._case = descriptor
        self._history_stage = history_stage
        self._rubric_stage = factory.openai_stage(OpenAIRoleV1.RUBRIC)
        self._descriptor_sha256 = transport_descriptor_sha256(live_descriptor)
        self._attempt_cost_ceiling = budget_ledger.attempt_ceiling(descriptor)
        self._attempt_sink = MemoryLiveAttemptReceiptSinkV1()
        self._history_runner = ProductionOpenAIAttemptRunnerV1(
            factory=factory,
            role=LiveAttemptRoleV1.HISTORY_POLICY,
            sink=self._attempt_sink,
            pricing=pricing,
            confirmed_pricing_sha256=pricing_sha256,
        )
        rubric_runner = ProductionOpenAIAttemptRunnerV1(
            factory=factory,
            role=LiveAttemptRoleV1.RUBRIC,
            sink=self._attempt_sink,
            pricing=pricing,
            confirmed_pricing_sha256=pricing_sha256,
        )
        rubric_port = ProductionRubricProviderPortV1(runner=rubric_runner)
        self._rubric_backend = LiveOpenAIRubricBackendV1(provider_port=rubric_port)
        self._coordinator = R24RuntimeCoordinatorV1(
            collector=CollectorEvidenceFactoryV1(),
            session_factory=self._new_rubric_session,
            rubric_call_observer=self._rubric_backend,
        )
        self._r22_receipts = MemoryR22PolicyReceiptSink()
        self._metrics = R22PolicyMetrics()
        self._call_inputs: dict[str, str] = {}
        self._call_indices: dict[str, int] = {}
        self._outputs: dict[str, RuntimeVerticalPolicyOutputV1] = {}
        self._bindings: dict[str, ResolvedLivePolicyCallBindingV1] = {}
        self._history_attempt_request_anchors: dict[
            str, LiveHistoryPolicyAttemptRequestAnchorV1
        ] = {}
        self._failures: dict[str, str] = {}
        self._case_deadline_ns = case_deadline_monotonic_ns
        self._lock = Lock()

    def _new_rubric_session(self, task_run_id: str, task: TaskInstructionV1) -> RubricTaskSession:
        return RubricTaskSession(
            task_run_id=task_run_id,
            task=task,
            builder_backend=self._rubric_backend,
            tracker_backend=self._rubric_backend,
        )

    @property
    def policy_id(self) -> str:
        return self._policy_id

    @property
    def execution_authority_sha256(self) -> str:
        return self._authority.manifest_sha256

    @property
    def source_transport_descriptor_sha256(self) -> str:
        return self._descriptor_sha256

    @property
    def source_transport_binding_sha256(self) -> str:
        raise R24ContractError(
            "PER_CALL_TRANSPORT_BINDING_REQUIRED",
            "a production transport binding exists only for one immutable actor call",
        )

    @property
    def case_descriptor(self) -> OwnerAuthorizedLiveCaseDescriptorV1:
        return self._case

    @contextmanager
    def bind_case_task_call(
        self,
        task_binding: TaskAuditBinding,
        *,
        actor_call_index: int,
        expected_actor_request_sha256: str | None = None,
    ) -> Iterator[None]:
        """Bind one exact Collector task attempt to one production actor call.

        This is ambient execution state, not caller-provided Sentinel metadata.
        The BaseAgent hook reads it only for this exact policy instance, and
        evaluation rechecks the currently bound Collector recorder/step.
        """

        if type(task_binding) is not TaskAuditBinding:
            raise R24ContractError(
                "COLLECTOR_TASK_BINDING_REQUIRED", "exact Collector task binding is required"
            )
        metadata = task_binding.metadata
        recorder = task_binding.task_recorder
        if (
            type(actor_call_index) is not int
            or not 1 <= actor_call_index <= self._case.max_actor_calls
            or metadata.task_run_id != getattr(recorder, "task_run_id", None)
            or not getattr(recorder, "enabled", False)
        ):
            raise R24ContractError(
                "COLLECTOR_TASK_BINDING_REQUIRED", "Collector task binding metadata differs"
            )
        if expected_actor_request_sha256 is not None:
            _require_sha256(expected_actor_request_sha256, "expected_actor_request_sha256")
        with self._lock:
            if actor_call_index != len(self._call_inputs) + 1:
                raise R24ContractError(
                    "CASE_ACTOR_CALL_ORDER_MISMATCH", "actor call index is not the next case call"
                )
        if _AMBIENT_PRODUCTION_CASE_CALL.get() is not None:
            raise R24ContractError(
                "NESTED_CASE_CALL_FORBIDDEN", "production case-call bindings cannot nest"
            )
        ambient = _AmbientProductionCaseCallV1(
            policy_id=self._policy_id,
            factory_binding_sha256=self._factory.factory_binding_sha256,
            task_run_id=metadata.task_run_id,
            task_recorder=recorder,
            actor_call_index=actor_call_index,
            expected_actor_request_sha256=expected_actor_request_sha256,
        )
        token = _AMBIENT_PRODUCTION_CASE_CALL.set(ambient)
        try:
            yield
        finally:
            _AMBIENT_PRODUCTION_CASE_CALL.reset(token)

    def current_case_context_attributes(self) -> dict[str, JsonValue]:
        """Return trusted request-external metadata for BaseAgent's common hook."""

        ambient = _AMBIENT_PRODUCTION_CASE_CALL.get()
        audit_context = get_audit_context()
        if (
            ambient is None
            or ambient.policy_id != self._policy_id
            or ambient.factory_binding_sha256 != self._factory.factory_binding_sha256
            or audit_context is None
            or audit_context.task_run_id != ambient.task_run_id
            or audit_context.recorder is not ambient.task_recorder
            or audit_context.step_id is None
        ):
            raise R24ContractError(
                "COLLECTOR_TASK_BINDING_REQUIRED",
                "live actor call is not inside its exact Collector task/step scope",
            )
        return {
            "r24_actor_call_index": ambient.actor_call_index,
            "r24_expected_actor_request_sha256": ambient.expected_actor_request_sha256,
            "r24_case_id": self._case.case_id,
            "r24_case_deadline_monotonic_ns": self._case_deadline_ns,
            "r24_mode": self._case.mode.value,
            "r24_reset_seed": self._case.reset_seed,
            "r24_stage": self._case.stage.value,
            "r24_task_id": self._case.task_id,
            "r24_task_parameters_sha256": self._case.task_parameters_sha256,
            "r24_task_run_id": ambient.task_run_id,
        }

    @staticmethod
    def _host_id(host: PilotHostV1) -> str:
        return (
            "mobileworld.qwen3vl.actor"
            if host is PilotHostV1.QWEN3_VL
            else "mobileworld.mai-ui.actor"
        )

    @staticmethod
    def _input_sha256(
        request: JsonValue, context: SentinelContext, history_ir: HistoryIR
    ) -> tuple[str, str]:
        if type(context) is not SentinelContext or type(history_ir) is not HistoryIR:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "per-call input type differs")
        actor_request_sha256 = portable_canonical_sha256(request)
        return actor_request_sha256, canonical_sha256(
            cast(
                JsonValue,
                {
                    "actor_request_sha256": actor_request_sha256,
                    "context": {
                        "attributes": context.attributes,
                        "host_id": context.host_id,
                        "logical_call_id": context.logical_call_id,
                    },
                    "history_ir_sha256": portable_canonical_sha256(
                        cast(JsonValue, history_ir.to_dict())
                    ),
                },
            )
        )

    @staticmethod
    def _no_history_input_sha256(request: JsonValue, context: SentinelContext) -> tuple[str, str]:
        if type(context) is not SentinelContext:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "per-call context type differs")
        actor_request_sha256 = portable_canonical_sha256(request)
        return actor_request_sha256, canonical_sha256(
            cast(
                JsonValue,
                {
                    "actor_request_sha256": actor_request_sha256,
                    "context": {
                        "attributes": context.attributes,
                        "host_id": context.host_id,
                        "logical_call_id": context.logical_call_id,
                    },
                    "history_status": "NO_HISTORY",
                },
            )
        )

    def _current_attempt_constraint(self) -> LiveRubricAttemptConstraintBindingV1:
        issued_ns = monotonic_ns()
        if self._case_deadline_ns <= issued_ns:
            raise R24ContractError("CASE_DEADLINE_EXCEEDED", "case wall deadline elapsed")
        try:
            return build_live_rubric_attempt_constraint_binding_v1(
                issued_monotonic_ns=issued_ns,
                case_execution_deadline_monotonic_ns=self._case_deadline_ns,
                history_stage=self._history_stage,
                rubric_stage=self._rubric_stage,
                case_stage=self._case.stage.value,
                case_host=self._case.host.value,
                case_mode=self._case.mode.value,
                case_id=self._case.case_id,
                task_id=self._case.task_id,
                task_parameters_sha256=self._case.task_parameters_sha256,
                reset_seed=self._case.reset_seed,
                max_actor_calls=self._case.max_actor_calls,
                max_openai_calls=self._case.max_openai_calls,
                max_wall_time_seconds=self._case.max_wall_time_seconds,
                case_max_cost_usd_micros=self._case.max_cost_usd_micros,
            )
        except Exception as exc:
            raise R24ContractError(
                "INVALID_ATTEMPT_CONSTRAINT",
                "live attempt deadline/cost authority could not be sealed",
            ) from exc

    def _require_case_context(
        self,
        context: SentinelContext,
        *,
        actor_call_index: int,
        actor_request_sha256: str,
    ) -> str:
        """Bind the manifest case to the active Collector task before any attempt."""

        attributes = context.attributes
        expected: dict[str, JsonValue] = {
            "r24_actor_call_index": actor_call_index,
            "r24_case_deadline_monotonic_ns": self._case_deadline_ns,
            "r24_case_id": self._case.case_id,
            "r24_mode": self._case.mode.value,
            "r24_reset_seed": self._case.reset_seed,
            "r24_stage": self._case.stage.value,
            "r24_task_id": self._case.task_id,
            "r24_task_parameters_sha256": self._case.task_parameters_sha256,
        }
        if any(attributes.get(name) != value for name, value in expected.items()):
            raise R24ContractError(
                "CASE_TASK_CONTEXT_MISMATCH",
                "actor context differs from the owner-pinned task/cell authority",
            )
        task_run_id = attributes.get("r24_task_run_id")
        if type(task_run_id) is not str or not task_run_id:
            raise R24ContractError(
                "CASE_TASK_CONTEXT_MISMATCH", "actor context lacks an exact Collector task run"
            )
        ambient = _AMBIENT_PRODUCTION_CASE_CALL.get()
        audit_context = get_audit_context()
        if (
            ambient is None
            or ambient.policy_id != self._policy_id
            or ambient.actor_call_index != actor_call_index
            or ambient.task_run_id != task_run_id
            or attributes.get("r24_expected_actor_request_sha256")
            != ambient.expected_actor_request_sha256
            or (
                ambient.expected_actor_request_sha256 is not None
                and ambient.expected_actor_request_sha256 != actor_request_sha256
            )
            or audit_context is None
            or audit_context.task_run_id != task_run_id
            or audit_context.recorder is not ambient.task_recorder
            or audit_context.step_id is None
        ):
            raise R24ContractError(
                "COLLECTOR_TASK_BINDING_REQUIRED",
                "Sentinel context is not backed by the active Collector task/step",
            )
        return task_run_id

    def call_binding(self, logical_call_id: str) -> ResolvedLivePolicyCallBindingV1:
        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            value = self._bindings.get(logical_call_id)
            if value is None:
                raise R24ContractError(
                    "PER_CALL_BINDING_UNAVAILABLE", "logical call has no completed live binding"
                )
            return value

    def actor_call_index_for_call(self, logical_call_id: str) -> int:
        """Return the immutable per-case call index assigned before live work."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            indices = getattr(self, "_call_indices", None)
            value = None if type(indices) is not dict else indices.get(logical_call_id)
            if type(value) is not int or value <= 0:
                raise R24ContractError(
                    "PER_CALL_INDEX_UNAVAILABLE", "logical call was never registered"
                )
            return value

    def attempt_receipts_for_call(self, logical_call_id: str) -> tuple[LiveAttemptReceiptV1, ...]:
        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            return tuple(
                snapshot_live_attempt_receipt(value)
                for value in self._attempt_sink.receipts
                if value.logical_call_id == logical_call_id
            )

    def rubric_call_receipts_for_call(
        self,
        logical_call_id: str,
    ) -> tuple[LiveRubricCallReceiptV1, ...]:
        """Return detached R2.4 rubric transport receipts for one actor call."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            backend = getattr(self, "_rubric_backend", None)
            if type(backend) is not LiveOpenAIRubricBackendV1:
                raise R24ContractError(
                    "RUBRIC_RECEIPTS_UNAVAILABLE",
                    "live rubric backend is unavailable",
                )
            return tuple(
                snapshot_live_rubric_call_receipt(value)
                for value in backend.call_receipts_for_call(logical_call_id)
            )

    def rubric_call_trust_anchors_for_call(
        self,
        logical_call_id: str,
    ) -> tuple[LiveRubricCallTrustAnchorV1, ...]:
        """Return exact ephemeral Collector/Responses preimages for validation."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            backend = getattr(self, "_rubric_backend", None)
            if type(backend) is not LiveOpenAIRubricBackendV1:
                raise R24ContractError(
                    "RUBRIC_TRUST_ANCHORS_UNAVAILABLE",
                    "live rubric backend is unavailable",
                )
            return tuple(
                snapshot_live_rubric_call_trust_anchor(value)
                for value in backend.call_trust_anchors_for_call(logical_call_id)
            )

    def rubric_attempt_request_anchors_for_call(
        self,
        logical_call_id: str,
    ) -> tuple[LiveRubricAttemptRequestAnchorV1, ...]:
        """Return request anchors registered before each live rubric transport."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            backend = getattr(self, "_rubric_backend", None)
            if type(backend) is not LiveOpenAIRubricBackendV1:
                raise R24ContractError(
                    "RUBRIC_TRUST_ANCHORS_UNAVAILABLE",
                    "live rubric backend is unavailable",
                )
            return tuple(
                snapshot_live_rubric_attempt_request_anchor(value)
                for value in backend.attempt_request_anchors_for_call(logical_call_id)
            )

    def history_policy_attempt_request_anchor_for_call(
        self,
        logical_call_id: str,
    ) -> LiveHistoryPolicyAttemptRequestAnchorV1 | None:
        """Return a detached full history attempt proof when one was formed."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            value = self._history_attempt_request_anchors.get(logical_call_id)
            return (
                None
                if value is None
                else snapshot_live_history_policy_attempt_request_anchor(value)
            )

    def history_evidence_packet_sha256_for_call(
        self,
        logical_call_id: str,
    ) -> str | None:
        """Return the Coordinator-owned R2.2 evidence root independently."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            record = self._coordinator.record_for(logical_call_id)
            anchor = self._history_attempt_request_anchors.get(logical_call_id)
            root = None if record is None else record.gpt56_evidence_packet_sha256
            if anchor is not None and root is None:
                raise R24ContractError(
                    "HISTORY_EVIDENCE_ROOT_UNAVAILABLE",
                    "formed history attempt lacks its Coordinator evidence root",
                )
            return None if root is None else _require_sha256(root, "evidence_packet_sha256")

    def _close_history_attempt_proof(
        self,
        *,
        attempt_id: str,
        logical_call_id: str,
        actor_request_sha256: str,
        constraint_binding: LiveRubricAttemptConstraintBindingV1,
        case_lease: CaseExecutionLeaseV1,
        transport_binding: OpenAIResponsesTransportBindingV1 | None,
        bridge: _PerCallAdmissionBridgeV1,
        r22_receipt_start_index: int,
        r22_receipt_failure_code: str | None,
    ) -> None:
        """Close a formed history attempt, or discard an unused registration."""

        receipt = self._history_runner.terminal_receipt_for_attempt(attempt_id)
        if receipt is None:
            discarded = self._history_runner.discard_unformed_history_attempt_constraint(
                attempt_id=attempt_id,
                logical_call_id=logical_call_id,
            )
            if not discarded and (
                self._history_runner.attempt_authority_for_attempt(attempt_id) is not None
                or self._history_runner.canonical_request_for_attempt(attempt_id) is not None
                or self._history_runner.attempt_deadline_binding_for_attempt(attempt_id) is not None
            ):
                raise R24ContractError(
                    "INCOMPLETE_HISTORY_ATTEMPT_PROOF",
                    "formed history attempt has no terminal receipt",
                )
            return
        authority = self._history_runner.attempt_authority_for_attempt(attempt_id)
        request = self._history_runner.canonical_request_for_attempt(attempt_id)
        deadline = self._history_runner.attempt_deadline_binding_for_attempt(attempt_id)
        evidence_sha256 = bridge.evidence_packet_sha256
        response = self._history_runner.response_envelope_for_attempt(attempt_id)
        r22_receipts = tuple(
            item
            for item in self._r22_receipts.receipts[r22_receipt_start_index:]
            if item.logical_call_id == logical_call_id
        )
        if (
            type(authority) is not LiveAttemptAuthorityV1
            or type(request) is not CanonicalHistoryPolicyRequestV1
            or type(deadline) is not LiveAttemptDeadlineBindingV1
            or type(transport_binding) is not OpenAIResponsesTransportBindingV1
            or type(evidence_sha256) is not str
            or response is not None
            and type(response) is not ResponsesEnvelopeV1
            or len(r22_receipts) > 1
        ):
            raise R24ContractError(
                "INCOMPLETE_HISTORY_ATTEMPT_PROOF",
                "formed history attempt lacks an exact proof preimage",
            )
        r22_receipt = None if not r22_receipts else r22_receipts[0]
        if r22_receipt is not None:
            r22_receipt_failure_code = None
        receipt_state = (
            LiveHistoryPolicyReceiptStateV1.R22_RECEIPT_COMMITTED
            if r22_receipt is not None
            else (
                LiveHistoryPolicyReceiptStateV1.R22_RECEIPT_ABSENT_PRE_DISPATCH
                if receipt.dispatch_count == 0
                else LiveHistoryPolicyReceiptStateV1.R22_RECEIPT_ABSENT_POST_DISPATCH
            )
        )
        anchor = _build_live_history_policy_attempt_request_anchor_v1(
            attempt_id=attempt_id,
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
            coordinator_evidence_packet_sha256=evidence_sha256,
            attempt_authority=authority,
            deadline_binding=deadline,
            constraint_binding=constraint_binding,
            case_execution_lease=case_lease,
            openai_stage=self._history_stage,
            rubric_openai_stage=self._rubric_stage,
            pricing=self._pricing,
            transport_binding=transport_binding,
            provider_request=request,
            response_envelope=response,
            r22_policy_receipt=r22_receipt,
            r22_receipt_state=receipt_state,
            r22_receipt_failure_code=r22_receipt_failure_code,
        )
        validate_live_history_policy_attempt_request_anchor_v1(anchor, attempt_receipt=receipt)
        if logical_call_id in self._history_attempt_request_anchors:
            raise R24ContractError(
                "DUPLICATE_HISTORY_ATTEMPT_PROOF",
                "logical call already has a history attempt proof",
            )
        self._history_attempt_request_anchors[logical_call_id] = anchor

    def rubric_collector_stimulus_sha256_for_call(
        self,
        logical_call_id: str,
    ) -> str | None:
        """Resolve the independent Coordinator-owned Collector root for a call.

        A failed attempt can occur before the Coordinator has a completed
        record.  The Coordinator therefore retains this root before rubric
        dispatch; proof publication never falls back to a self-attested
        provider-anchor hash.
        """

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            record = self._coordinator.record_for(logical_call_id)
            coordinator_root = self._coordinator.stimulus_sha256_for_call(logical_call_id)
            if coordinator_root is not None:
                trusted_root = _require_sha256(coordinator_root, "history_free_stimulus_sha256")
                if record is not None and record.history_free_stimulus_sha256 != trusted_root:
                    raise R24ContractError(
                        "RUBRIC_COLLECTOR_ROOT_MISMATCH",
                        "Coordinator record and pre-dispatch Collector root differ",
                    )
                return trusted_root
            backend = getattr(self, "_rubric_backend", None)
            anchors = (
                ()
                if type(backend) is not LiveOpenAIRubricBackendV1
                else backend.attempt_request_anchors_for_call(logical_call_id)
            )
            if anchors:
                raise R24ContractError(
                    "RUBRIC_COLLECTOR_ROOT_UNAVAILABLE",
                    "completed rubric call lacks its Coordinator Collector root",
                )
            return None

    def rubric_backend_extension_descriptor(
        self,
    ) -> R24RubricBackendExtensionDescriptorV1:
        """Return the detached R2.4 transport descriptor bound by call receipts."""

        with self._lock:
            backend = getattr(self, "_rubric_backend", None)
            if type(backend) is not LiveOpenAIRubricBackendV1:
                raise R24ContractError(
                    "RUBRIC_DESCRIPTOR_UNAVAILABLE",
                    "live rubric backend descriptor is unavailable",
                )
            return snapshot_r24_rubric_backend_extension_descriptor(backend.extension_descriptor)

    def rubric_tracking_packet_sha256_for_call(
        self,
        logical_call_id: str,
    ) -> str | None:
        """Resolve the Coordinator-owned complete tracking-packet root."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            record = self._coordinator.record_for(logical_call_id)
            coordinator_root = self._coordinator.tracking_packet_sha256_for_call(logical_call_id)
            backend = getattr(self, "_rubric_backend", None)
            anchors = (
                ()
                if type(backend) is not LiveOpenAIRubricBackendV1
                else backend.attempt_request_anchors_for_call(logical_call_id)
            )
            has_track_anchor = any(
                item.operation is LiveRubricOperationV1.TRACK for item in anchors
            )
            if coordinator_root is None:
                if has_track_anchor:
                    raise R24ContractError(
                        "RUBRIC_TRACKING_PACKET_ROOT_UNAVAILABLE",
                        "completed tracking call lacks its Coordinator packet root",
                    )
                return None
            trusted_root = _require_sha256(coordinator_root, "tracking_packet_sha256")
            if (
                record is not None
                and record.tracking_packet_sha256 is not None
                and record.tracking_packet_sha256 != trusted_root
            ):
                raise R24ContractError(
                    "RUBRIC_TRACKING_PACKET_ROOT_MISMATCH",
                    "Coordinator record and pre-dispatch tracking root differ",
                )
            return trusted_root

    def failure_for_call(self, logical_call_id: str) -> str | None:
        """Expose a stable failure code without requiring a successful binding."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._call_inputs:
                raise R24ContractError(
                    "PER_CALL_ATTEMPTS_UNAVAILABLE", "logical call was never registered"
                )
            return self._failures.get(logical_call_id)

    def coordinated_record_for_call(self, logical_call_id: str) -> R24CoordinatedCallRecordV1:
        """Return the coordinator's detached R2.3/R2.2 record for one completed call."""

        _require_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id not in self._bindings:
                raise R24ContractError(
                    "PER_CALL_BINDING_UNAVAILABLE", "logical call has no completed live binding"
                )
            record = self._coordinator.record_for(logical_call_id)
            if record is None:
                raise R24ContractError(
                    "COORDINATED_CALL_RECORD_UNAVAILABLE",
                    "completed live call has no coordinated record",
                )
            return record

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> RuntimeVerticalPolicyOutputV1:
        control = _DirectLivePolicyExecutionControl(self._history_stage.timeout_ms)
        return self.evaluate_with_control(
            request=request,
            context=context,
            history_ir=history_ir,
            execution_control=control,
        )

    def prepare_no_history_with_control(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        execution_control: PolicyExecutionControlV1,
    ) -> R24CoordinatedCallRecordV1:
        """Run only rubric generation/tracking for one typed no-history call.

        This is the sole production no-history semantic path. It never builds an
        R2.2 packet or HISTORY_POLICY transport, and it caches the admitted
        rubric record under the logical call so provider/parse retries cannot
        repeat an OpenAI attempt.
        """

        if not isinstance(execution_control, PolicyExecutionControlV1):
            raise R24ContractError("UNTRUSTED_EXECUTION_CONTROL", "execution control differs")
        actor_request_sha256, input_sha256 = self._no_history_input_sha256(request, context)
        if context.host_id != self._host_id(self._case.host):
            raise R24ContractError("CASE_HOST_MISMATCH", "actor context binds another host")
        with self._lock:
            prior_input = self._call_inputs.get(context.logical_call_id)
            if prior_input is not None:
                if prior_input != input_sha256:
                    raise R24ContractError(
                        "LOGICAL_CALL_INPUT_DRIFT", "logical call was reused with changed input"
                    )
                binding = self._bindings.get(context.logical_call_id)
                if binding is not None and binding.output_sha256 is None:
                    record = self._coordinator.record_for(context.logical_call_id)
                    if record is None:
                        raise R24ContractError(
                            "COORDINATED_CALL_RECORD_UNAVAILABLE",
                            "completed no-history call has no rubric record",
                        )
                    return record
                failure = self._failures.get(context.logical_call_id)
                if failure is not None:
                    raise R24ContractError(failure, "cached per-call live failure")
                raise R24ContractError("LOGICAL_CALL_BUSY", "logical call is already evaluating")
            actor_call_index = len(self._call_inputs) + 1
            if actor_call_index > self._case.max_actor_calls:
                raise R24ContractError("CASE_ACTOR_CALL_BUDGET_EXCEEDED", "actor call cap reached")
            self._call_inputs[context.logical_call_id] = input_sha256
            self._call_indices[context.logical_call_id] = actor_call_index
            reservation: _LiveBudgetReservationV1 | None = None
            try:
                task_run_id = self._require_case_context(
                    context,
                    actor_call_index=actor_call_index,
                    actor_request_sha256=actor_request_sha256,
                )
                expected_rubric = 2 if actor_call_index == 1 else 1
                reservation = self._budget_ledger.reserve_call(
                    self._case,
                    logical_call_id=context.logical_call_id,
                    actor_call_index=actor_call_index,
                    attempt_count=expected_rubric,
                )
                constraint_binding = self._current_attempt_constraint()
                deadline_ns = constraint_binding.effective_deadline_monotonic_ns
                case_lease = self._factory.issue_case_execution_lease(
                    stage=self._case.stage,
                    host=self._case.host,
                    mode=self._case.mode,
                    case_id=self._case.case_id,
                    task_id=self._case.task_id,
                    task_parameters_sha256=self._case.task_parameters_sha256,
                    reset_seed=self._case.reset_seed,
                    actor_call_index=actor_call_index,
                    request_sha256=actor_request_sha256,
                )
                if type(case_lease) is not CaseExecutionLeaseV1:
                    raise R24ContractError("CASE_LEASE_REQUIRED", "factory lease type differs")
                if (
                    case_lease.task_id != self._case.task_id
                    or case_lease.task_parameters_sha256 != self._case.task_parameters_sha256
                    or case_lease.reset_seed != self._case.reset_seed
                    or case_lease.actor_call_index != actor_call_index
                    or case_lease.request_sha256 != actor_request_sha256
                ):
                    raise R24ContractError(
                        "CASE_LEASE_TASK_BINDING_MISMATCH",
                        "factory lease differs from the exact actor task/call binding",
                    )
                case_lease_hash = case_execution_lease_sha256(case_lease)
                before_receipts = len(self._attempt_sink.receipts)
                self._rubric_backend.bind_case_authority(
                    case_lease=case_lease,
                    logical_call_id=context.logical_call_id,
                    actor_request_sha256=actor_request_sha256,
                    deadline_monotonic_ns=deadline_ns,
                    max_cost_usd_micros=self._attempt_cost_ceiling,
                    constraint_binding=constraint_binding,
                    execution_control=execution_control,
                )
                record = self._coordinator.prepare_no_history(request, context)
                receipts = self._attempt_sink.receipts[before_receipts:]
                if record.task_run_id != task_run_id:
                    raise R24ContractError(
                        "COLLECTOR_TASK_AUTHORITY_MISMATCH",
                        "Collector rubric evidence binds another task run",
                    )
                if any(value.cost_usd_micros is None for value in receipts):
                    raise R24ContractError(
                        "INCOMPLETE_ATTEMPT_PROOF",
                        "no-history rubric attempt cost is unavailable",
                    )
                rubric_attempt_hashes = tuple(
                    live_attempt_receipt_sha256(value)
                    for value in receipts
                    if value.role is LiveAttemptRoleV1.RUBRIC
                )
                rubric_call_receipts = self._rubric_backend.call_receipts_for_call(
                    context.logical_call_id
                )
                rubric_call_trust_anchors = self._rubric_backend.call_trust_anchors_for_call(
                    context.logical_call_id
                )
                rubric_attempt_request_anchors = (
                    self._rubric_backend.attempt_request_anchors_for_call(context.logical_call_id)
                )
                rubric_extension = self._rubric_backend.extension_descriptor
                exact_cost_usd_micros = sum(cast(int, value.cost_usd_micros) for value in receipts)
                binding = ResolvedLivePolicyCallBindingV1(
                    logical_call_id=context.logical_call_id,
                    actor_call_index=actor_call_index,
                    actor_request_sha256=actor_request_sha256,
                    policy_id=self._policy_id,
                    execution_authority_sha256=self.execution_authority_sha256,
                    source_transport_descriptor_sha256=self._descriptor_sha256,
                    source_transport_binding_sha256=None,
                    case_execution_lease_sha256=case_lease_hash,
                    preflight_report_sha256=self._factory.preflight_report_sha256,
                    factory_binding_sha256=self._factory.factory_binding_sha256,
                    pricing_binding_sha256=self._factory.pricing_binding_sha256,
                    rubric_backend_extension_descriptor_sha256=(rubric_extension.sha256),
                    rubric_attempt_receipt_sha256s=rubric_attempt_hashes,
                    rubric_call_receipt_sha256s=tuple(
                        live_rubric_call_receipt_sha256(value) for value in rubric_call_receipts
                    ),
                    history_policy_attempt_receipt_sha256=None,
                    output_sha256=None,
                    openai_calls=len(receipts),
                    cost_usd_micros=exact_cost_usd_micros,
                )
                validate_live_rubric_cross_bindings_v1(
                    logical_call_id=context.logical_call_id,
                    actor_request_sha256=actor_request_sha256,
                    attempts=receipts,
                    rubric_attempt_request_anchors=rubric_attempt_request_anchors,
                    rubric_call_receipts=rubric_call_receipts,
                    rubric_call_trust_anchors=rubric_call_trust_anchors,
                    expected_collector_stimulus_sha256=(record.history_free_stimulus_sha256),
                    expected_tracking_packet_sha256=record.tracking_packet_sha256,
                    rubric_backend_extension=rubric_extension,
                    binding=binding,
                    actor_call_index=actor_call_index,
                    expect_history_policy=False,
                    allow_incomplete=False,
                )
                self._budget_ledger.settle_call(
                    reservation,
                    exact_cost_usd_micros=exact_cost_usd_micros,
                )
                reservation = None
                self._bindings[context.logical_call_id] = binding
                return record
            except Exception as exc:
                if reservation is not None:
                    try:
                        self._budget_ledger.freeze_failed_call(reservation)
                    except Exception:
                        pass
                code = getattr(exc, "code", "PER_CALL_LIVE_EVALUATION_FAILED")
                if type(code) is not str or not code:
                    code = "PER_CALL_LIVE_EVALUATION_FAILED"
                self._failures[context.logical_call_id] = code
                if isinstance(exc, R24ContractError):
                    raise
                raise R24ContractError(code, "per-call no-history rubric failed closed") from exc

    def evaluate_with_control(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        execution_control: PolicyExecutionControlV1,
    ) -> RuntimeVerticalPolicyOutputV1:
        if not isinstance(execution_control, PolicyExecutionControlV1):
            raise R24ContractError("UNTRUSTED_EXECUTION_CONTROL", "execution control differs")
        actor_request_sha256, input_sha256 = self._input_sha256(request, context, history_ir)
        if context.host_id != self._host_id(self._case.host):
            raise R24ContractError("CASE_HOST_MISMATCH", "actor context binds another host")
        with self._lock:
            prior_input = self._call_inputs.get(context.logical_call_id)
            if prior_input is not None:
                if prior_input != input_sha256:
                    raise R24ContractError(
                        "LOGICAL_CALL_INPUT_DRIFT", "logical call was reused with changed input"
                    )
                output = self._outputs.get(context.logical_call_id)
                if output is not None:
                    return snapshot_vertical_output(output)
                failure = self._failures.get(context.logical_call_id)
                if failure is not None:
                    raise R24ContractError(failure, "cached per-call live failure")
                raise R24ContractError("LOGICAL_CALL_BUSY", "logical call is already evaluating")
            actor_call_index = len(self._call_inputs) + 1
            if actor_call_index > self._case.max_actor_calls:
                raise R24ContractError("CASE_ACTOR_CALL_BUDGET_EXCEEDED", "actor call cap reached")
            self._call_inputs[context.logical_call_id] = input_sha256
            self._call_indices[context.logical_call_id] = actor_call_index
            reservation: _LiveBudgetReservationV1 | None = None
            try:
                task_run_id = self._require_case_context(
                    context,
                    actor_call_index=actor_call_index,
                    actor_request_sha256=actor_request_sha256,
                )
                expected_attempt_count = 3 if actor_call_index == 1 else 2
                reservation = self._budget_ledger.reserve_call(
                    self._case,
                    logical_call_id=context.logical_call_id,
                    actor_call_index=actor_call_index,
                    attempt_count=expected_attempt_count,
                )
                constraint_binding = self._current_attempt_constraint()
                deadline_ns = constraint_binding.effective_deadline_monotonic_ns
                case_lease = self._factory.issue_case_execution_lease(
                    stage=self._case.stage,
                    host=self._case.host,
                    mode=self._case.mode,
                    case_id=self._case.case_id,
                    task_id=self._case.task_id,
                    task_parameters_sha256=self._case.task_parameters_sha256,
                    reset_seed=self._case.reset_seed,
                    actor_call_index=actor_call_index,
                    request_sha256=actor_request_sha256,
                )
                if type(case_lease) is not CaseExecutionLeaseV1:
                    raise R24ContractError("CASE_LEASE_REQUIRED", "factory lease type differs")
                if (
                    case_lease.task_id != self._case.task_id
                    or case_lease.task_parameters_sha256 != self._case.task_parameters_sha256
                    or case_lease.reset_seed != self._case.reset_seed
                    or case_lease.actor_call_index != actor_call_index
                    or case_lease.request_sha256 != actor_request_sha256
                ):
                    raise R24ContractError(
                        "CASE_LEASE_TASK_BINDING_MISMATCH",
                        "factory lease differs from the exact actor task/call binding",
                    )
                case_lease_hash = case_execution_lease_sha256(case_lease)
                before_receipts = len(self._attempt_sink.receipts)
                self._rubric_backend.bind_case_authority(
                    case_lease=case_lease,
                    logical_call_id=context.logical_call_id,
                    actor_request_sha256=actor_request_sha256,
                    deadline_monotonic_ns=deadline_ns,
                    max_cost_usd_micros=self._attempt_cost_ceiling,
                    constraint_binding=constraint_binding,
                    execution_control=execution_control,
                )
                timeout_seconds = self._history_stage.timeout_ms / 1_000
                client_timeout_seconds = timeout_seconds / 2
                attempt_id = (
                    "r24-history-"
                    + hashlib.sha256(
                        (
                            context.logical_call_id + actor_request_sha256 + str(actor_call_index)
                        ).encode()
                    ).hexdigest()[:32]
                )
                bridge = _PerCallAdmissionBridgeV1(self._coordinator, seal=_PER_CALL_POLICY_SEAL)
                if self._attempt_cost_ceiling != constraint_binding.attempt_max_cost_usd_micros:
                    raise R24ContractError(
                        "ATTEMPT_COST_AUTHORITY_MISMATCH",
                        "budget ledger and sealed case constraint differ",
                    )
                self._history_runner.register_history_attempt_constraint(
                    case_lease=case_lease,
                    attempt_id=attempt_id,
                    logical_call_id=context.logical_call_id,
                    case_execution_deadline_monotonic_ns=(
                        constraint_binding.effective_deadline_monotonic_ns
                    ),
                    request_timeout_ns=round(client_timeout_seconds * 1_000_000_000),
                    max_cost_usd_micros=(constraint_binding.attempt_max_cost_usd_micros),
                )
                transport = None
                history_transport_binding = None
                r22_receipt_start_index = len(self._r22_receipts.receipts)
                r22_receipt_failure_code: str | None = None
                try:
                    transport = build_owner_authorized_openai_responses_transport(
                        attempt_runner=self._history_runner,
                        case_execution_lease=case_lease,
                        attempt_id=attempt_id,
                        logical_call_id=context.logical_call_id,
                        max_cost_usd_micros=(constraint_binding.attempt_max_cost_usd_micros),
                        seam_policy_deadline_seconds=timeout_seconds,
                        client_timeout_seconds=client_timeout_seconds,
                    )
                    source = GPT56SentinelPolicy(
                        transport=transport,
                        evidence_packet_factory=bridge.evidence,
                        proposal_admission=bridge.admit,
                        admission_receipt_projector=admission_receipt_projector,
                        bind_policy_receipt=bind_policy_receipt,
                        receipt_sink=self._r22_receipts,
                        metrics=self._metrics,
                        output_schema=ProposalSchemaSnapshotV1.from_checked_in(),
                        timeout_seconds=client_timeout_seconds,
                        seam_policy_deadline_seconds=timeout_seconds,
                        policy_id=f"{self._policy_id}.r22",
                    )
                    history_transport_binding = source.assert_live_transport_binding()
                    adapter = R22OwnerAuthorizedLivePolicyAdapter(
                        source,
                        authority=self._authority,
                        _per_call_policy_id=self._policy_id,
                        _per_call_seal=_PER_CALL_POLICY_SEAL,
                    )
                    try:
                        output = adapter.evaluate_with_control(
                            request=request,
                            context=context,
                            history_ir=history_ir,
                            execution_control=execution_control,
                        )
                    except GPT56PolicyError as exc:
                        if exc.code in _R22_RECEIPT_PUBLICATION_FAILURE_CODES:
                            r22_receipt_failure_code = exc.code
                        raise
                finally:
                    try:
                        if transport is not None:
                            transport.close()
                    finally:
                        self._close_history_attempt_proof(
                            attempt_id=attempt_id,
                            logical_call_id=context.logical_call_id,
                            actor_request_sha256=actor_request_sha256,
                            constraint_binding=constraint_binding,
                            case_lease=case_lease,
                            transport_binding=history_transport_binding,
                            bridge=bridge,
                            r22_receipt_start_index=r22_receipt_start_index,
                            r22_receipt_failure_code=r22_receipt_failure_code,
                        )
                receipts = self._attempt_sink.receipts[before_receipts:]
                coordinated_record = self._coordinator.record_for(context.logical_call_id)
                if coordinated_record is None or coordinated_record.task_run_id != task_run_id:
                    raise R24ContractError(
                        "COLLECTOR_TASK_AUTHORITY_MISMATCH",
                        "Collector evidence binds another task run",
                    )
                if any(value.cost_usd_micros is None for value in receipts):
                    raise R24ContractError(
                        "INCOMPLETE_ATTEMPT_PROOF", "live attempt cost is unavailable"
                    )
                rubric_receipts = tuple(
                    value for value in receipts if value.role is LiveAttemptRoleV1.RUBRIC
                )
                history_receipts = tuple(
                    value for value in receipts if value.role is LiveAttemptRoleV1.HISTORY_POLICY
                )
                if len(history_receipts) != 1:
                    raise R24ContractError(
                        "OPENAI_ROLE_CENSUS_MISMATCH", "rubric/history attempt census differs"
                    )
                history_request_anchor = self._history_attempt_request_anchors.get(
                    context.logical_call_id
                )
                if history_request_anchor is None:
                    raise R24ContractError(
                        "INCOMPLETE_HISTORY_ATTEMPT_PROOF",
                        "history attempt lacks its full request proof",
                    )
                if (
                    coordinated_record.gpt56_evidence_packet_sha256
                    != history_request_anchor.coordinator_evidence_packet_sha256
                ):
                    raise R24ContractError(
                        "HISTORY_REQUEST_PROOF_MISMATCH",
                        "history request differs from the Coordinator evidence root",
                    )
                rubric_attempt_hashes = tuple(
                    live_attempt_receipt_sha256(value) for value in rubric_receipts
                )
                rubric_call_receipts = self._rubric_backend.call_receipts_for_call(
                    context.logical_call_id
                )
                rubric_call_trust_anchors = self._rubric_backend.call_trust_anchors_for_call(
                    context.logical_call_id
                )
                rubric_attempt_request_anchors = (
                    self._rubric_backend.attempt_request_anchors_for_call(context.logical_call_id)
                )
                rubric_extension = self._rubric_backend.extension_descriptor
                exact_cost_usd_micros = sum(cast(int, value.cost_usd_micros) for value in receipts)
                transport_binding_sha256 = adapter.source_transport_binding_sha256
                binding = ResolvedLivePolicyCallBindingV1(
                    logical_call_id=context.logical_call_id,
                    actor_call_index=actor_call_index,
                    actor_request_sha256=actor_request_sha256,
                    policy_id=self._policy_id,
                    execution_authority_sha256=self.execution_authority_sha256,
                    source_transport_descriptor_sha256=self._descriptor_sha256,
                    source_transport_binding_sha256=transport_binding_sha256,
                    case_execution_lease_sha256=case_lease_hash,
                    preflight_report_sha256=self._factory.preflight_report_sha256,
                    factory_binding_sha256=self._factory.factory_binding_sha256,
                    pricing_binding_sha256=self._factory.pricing_binding_sha256,
                    rubric_backend_extension_descriptor_sha256=(rubric_extension.sha256),
                    rubric_attempt_receipt_sha256s=rubric_attempt_hashes,
                    rubric_call_receipt_sha256s=tuple(
                        live_rubric_call_receipt_sha256(value) for value in rubric_call_receipts
                    ),
                    history_policy_attempt_receipt_sha256=live_attempt_receipt_sha256(
                        history_receipts[0]
                    ),
                    output_sha256=vertical_output_sha256(output),
                    openai_calls=len(receipts),
                    cost_usd_micros=exact_cost_usd_micros,
                )
                validate_live_rubric_cross_bindings_v1(
                    logical_call_id=context.logical_call_id,
                    actor_request_sha256=actor_request_sha256,
                    attempts=receipts,
                    rubric_attempt_request_anchors=rubric_attempt_request_anchors,
                    rubric_call_receipts=rubric_call_receipts,
                    rubric_call_trust_anchors=rubric_call_trust_anchors,
                    expected_collector_stimulus_sha256=(
                        coordinated_record.history_free_stimulus_sha256
                    ),
                    expected_tracking_packet_sha256=(coordinated_record.tracking_packet_sha256),
                    rubric_backend_extension=rubric_extension,
                    binding=binding,
                    actor_call_index=actor_call_index,
                    expect_history_policy=True,
                    allow_incomplete=False,
                    history_policy_attempt_request_anchor=history_request_anchor,
                )
                self._budget_ledger.settle_call(
                    reservation,
                    exact_cost_usd_micros=exact_cost_usd_micros,
                )
                reservation = None
                if (
                    output.policy_id != self._policy_id
                    or output.execution_authority_sha256 != binding.execution_authority_sha256
                    or output.source_transport_descriptor_sha256
                    != binding.source_transport_descriptor_sha256
                    or output.source_transport_binding_sha256
                    != binding.source_transport_binding_sha256
                ):
                    raise R24ContractError(
                        "PER_CALL_OUTPUT_BINDING_MISMATCH", "live output differs from call proof"
                    )
                trusted_output = snapshot_vertical_output(output)
                self._bindings[context.logical_call_id] = binding
                self._outputs[context.logical_call_id] = trusted_output
                return snapshot_vertical_output(trusted_output)
            except Exception as exc:
                if reservation is not None:
                    try:
                        self._budget_ledger.freeze_failed_call(reservation)
                    except Exception:
                        pass
                code = getattr(exc, "code", "PER_CALL_LIVE_EVALUATION_FAILED")
                if type(code) is not str or not code:
                    code = "PER_CALL_LIVE_EVALUATION_FAILED"
                self._failures[context.logical_call_id] = code
                if isinstance(exc, R24ContractError):
                    raise
                raise R24ContractError(code, "per-call live evaluation failed closed") from exc


def build_owner_authorized_live_per_call_policy_v1(
    *,
    factory: ProductionPostPreflightFactoryV1,
    pricing: LiveAttemptPricingV1,
    budget_ledger: ProductionLiveBudgetLedgerV1,
    stage: RunStageV1,
    host: PilotHostV1,
    mode: SmokeModeV1,
    case_id: str,
    case_deadline_monotonic_ns: int,
) -> OwnerAuthorizedLivePerCallPolicyV1:
    """Build one exact case resolver without accepting executable dependencies."""

    return OwnerAuthorizedLivePerCallPolicyV1(
        factory=factory,
        pricing=pricing,
        budget_ledger=budget_ledger,
        stage=stage,
        host=host,
        mode=mode,
        case_id=case_id,
        case_deadline_monotonic_ns=case_deadline_monotonic_ns,
        seal=_PER_CALL_POLICY_SEAL,
    )


__all__ = [
    "LIVE_HISTORY_POLICY_REQUEST_PROOF_SCHEMA_VERSION",
    "OWNER_AUTHORIZED_LIVE_ACTIVE_SCOPE_VALUE",
    "OWNER_AUTHORIZED_LIVE_POLICY_AUTHORITY_SCHEMA_VERSION",
    "LiveHistoryPolicyAttemptRequestAnchorV1",
    "LiveHistoryPolicyReceiptStateV1",
    "OwnerAuthorizedLiveCaseDescriptorV1",
    "OwnerAuthorizedLivePerCallPolicyV1",
    "OwnerAuthorizedLivePolicyAuthorityV1",
    "R22OwnerAuthorizedLivePolicyAdapter",
    "ResolvedLivePolicyCallBindingV1",
    "ProductionLiveBudgetLedgerV1",
    "build_production_live_budget_ledger_v1",
    "build_owner_authorized_live_per_call_policy_v1",
    "history_policy_stage_sha256",
    "issue_owner_authorized_live_policy_authority",
    "live_history_policy_attempt_request_proof_projection",
    "owner_authorized_live_policy_authority_projection",
    "owner_authorized_live_policy_authority_sha256",
    "promote_owner_authorized_live_policy_output",
    "resolved_live_policy_call_binding_projection",
    "resolved_live_policy_call_binding_sha256",
    "snapshot_live_history_policy_attempt_request_anchor",
    "validate_live_history_policy_attempt_request_anchor_v1",
    "validate_live_history_policy_request_proof_projection_v1",
    "validate_live_rubric_cross_bindings_v1",
]
