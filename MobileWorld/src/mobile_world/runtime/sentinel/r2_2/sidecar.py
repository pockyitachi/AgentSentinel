"""Bounded, hash-only receipts for the R2.2 semantic-policy call.

This channel is deliberately separate from the R2.1 pre-call seam receipt.  It
binds the semantic request and response provenance without retaining evidence,
screenshots, model output, request views, or reasoning text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Lock
from typing import Any, Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import (
    JsonValue,
    canonical_json_bytes,
    canonical_sha256,
)

R22_POLICY_RECEIPT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-policy-receipt/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_SDK_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}")


class PolicyEvaluationStatus(StrEnum):
    """Closed, low-cardinality outcomes for one semantic-policy evaluation."""

    ADMITTED = "ADMITTED"
    EVIDENCE_REJECTED = "EVIDENCE_REJECTED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    ADMISSION_REJECTED = "ADMISSION_REJECTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class R22PolicyReceiptV1:
    """Safe provenance for one R2.2 policy call; all content is hash-only."""

    logical_call_id: str
    host_id: str
    packet_id: str | None
    policy_id: str
    execution_scope: str
    mode: str
    requested_model: str
    returned_model: str | None
    api_method: str
    openai_sdk_version: str
    reasoning_effort: str
    sdk_max_retries: int
    transport_kind: str
    transport_authority: str
    prompt_sha256: str
    output_schema_sha256: str
    request_config_sha256: str
    evidence_packet_sha256: str | None
    current_image_sha256: str | None
    response_id: str | None
    response_status: str | None
    service_tier: str | None
    response_envelope_sha256: str | None
    provider_output_sha256: str | None
    parsed_proposal_sha256: str | None
    admitted_plan_sha256: str | None
    evaluation_status: PolicyEvaluationStatus
    failure_code: str | None
    validation_checks: tuple[str, ...]
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    transport_calls: int
    packet_build_latency_ns: int
    transport_latency_ns: int
    parse_latency_ns: int
    admission_latency_ns: int
    total_latency_ns: int
    target_count: int
    decision_count: int
    keep_count: int
    drop_count: int
    replace_count: int
    keep_uncertain_count: int
    material_decision_count: int
    abstain_decision_count: int
    external_network_attempted: bool
    model_call_attempted: bool
    local_gpu_used: bool
    mobileworld_action_executed: bool
    evidence_persisted: bool = False
    screenshot_persisted: bool = False
    provider_output_persisted: bool = False
    reasoning_persisted: bool = False
    schema_version: str = R22_POLICY_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != R22_POLICY_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unknown R2.2 policy receipt schema version")
        if (
            type(self.logical_call_id) is not str
            or _RUNTIME_ID.fullmatch(self.logical_call_id) is None
        ):
            raise ValueError("logical_call_id must be a bounded path-safe ID")
        if type(self.host_id) is not str or _RUNTIME_ID.fullmatch(self.host_id) is None:
            raise ValueError("host_id must be a bounded path-safe ID")
        if self.packet_id is not None and (
            type(self.packet_id) is not str or _RUNTIME_ID.fullmatch(self.packet_id) is None
        ):
            raise ValueError("packet_id must be a bounded path-safe ID when present")
        if type(self.policy_id) is not str or _SEMANTIC_ID.fullmatch(self.policy_id) is None:
            raise ValueError("policy_id must be a bounded semantic ID")
        if self.execution_scope != "SHADOW_ONLY":
            raise ValueError("the R2.2 v1 semantic policy is SHADOW_ONLY")
        if self.mode != "SHADOW":
            raise ValueError("the R2.2 v1 policy receipt mode must be SHADOW")
        if self.requested_model != "gpt-5.6-sol":
            raise ValueError("the R2.2 v1 receipt must bind the pinned requested model")
        if self.returned_model is not None and (
            type(self.returned_model) is not str
            or _SEMANTIC_ID.fullmatch(self.returned_model) is None
        ):
            raise ValueError("returned_model must be a bounded semantic ID when present")
        if self.returned_model is not None and self.returned_model != self.requested_model:
            raise ValueError("returned_model must equal the pinned requested model")
        if self.api_method != "responses.create":
            raise ValueError("R2.2 v1 uses only the Responses create API")
        if (
            type(self.openai_sdk_version) is not str
            or _SDK_VERSION.fullmatch(self.openai_sdk_version) is None
        ):
            raise ValueError("openai_sdk_version must match the receipt schema")
        if self.reasoning_effort != "medium":
            raise ValueError("R2.2 v1 pins medium reasoning effort")
        if type(self.sdk_max_retries) is not int or self.sdk_max_retries != 0:
            raise ValueError("the dedicated R2.2 client must disable SDK retries")
        if self.transport_kind not in {"FAKE", "OPENAI_RESPONSES"}:
            raise ValueError("transport_kind must be a closed R2.2 transport label")
        if self.transport_authority not in {
            "CPU_OFFLINE_FAKE",
            "EXPLICIT_OWNER_AUTHORIZATION",
        }:
            raise ValueError("transport_authority must be a closed R2.2 authority label")
        if self.transport_kind == "FAKE" and self.transport_authority != "CPU_OFFLINE_FAKE":
            raise ValueError("fake transport must use CPU_OFFLINE_FAKE authority")
        if (
            self.transport_kind == "OPENAI_RESPONSES"
            and self.transport_authority != "EXPLICIT_OWNER_AUTHORIZATION"
        ):
            raise ValueError("OpenAI transport requires explicit owner authorization")
        for required_name, required_digest in (
            ("prompt_sha256", self.prompt_sha256),
            ("output_schema_sha256", self.output_schema_sha256),
            ("request_config_sha256", self.request_config_sha256),
        ):
            if type(required_digest) is not str or _SHA256.fullmatch(required_digest) is None:
                raise ValueError(f"{required_name} must be lowercase SHA-256")
        for optional_name, optional_digest in (
            ("evidence_packet_sha256", self.evidence_packet_sha256),
            ("current_image_sha256", self.current_image_sha256),
            ("response_envelope_sha256", self.response_envelope_sha256),
            ("provider_output_sha256", self.provider_output_sha256),
            ("parsed_proposal_sha256", self.parsed_proposal_sha256),
            ("admitted_plan_sha256", self.admitted_plan_sha256),
        ):
            if optional_digest is not None and (
                type(optional_digest) is not str or _SHA256.fullmatch(optional_digest) is None
            ):
                raise ValueError(f"{optional_name} must be lowercase SHA-256 when present")
        packet_bindings = (
            self.packet_id,
            self.evidence_packet_sha256,
            self.current_image_sha256,
        )
        if any(value is None for value in packet_bindings) and any(
            value is not None for value in packet_bindings
        ):
            raise ValueError("packet ID, evidence hash, and image hash must be present together")
        for name, value in (
            ("response_id", self.response_id),
            ("response_status", self.response_status),
            ("service_tier", self.service_tier),
        ):
            if value is not None and (
                type(value) is not str or _SEMANTIC_ID.fullmatch(value) is None
            ):
                raise ValueError(f"{name} must be a bounded safe value when present")
        if type(self.evaluation_status) is not PolicyEvaluationStatus:
            raise TypeError("evaluation_status must be PolicyEvaluationStatus")
        if self.failure_code is not None and (
            type(self.failure_code) is not str or _CODE.fullmatch(self.failure_code) is None
        ):
            raise ValueError("failure_code must be a bounded closed code when present")
        if self.evaluation_status is PolicyEvaluationStatus.ADMITTED:
            if self.failure_code is not None:
                raise ValueError("an admitted evaluation cannot carry a failure code")
            required_admitted_values = (
                self.packet_id,
                self.evidence_packet_sha256,
                self.current_image_sha256,
                self.returned_model,
                self.response_id,
                self.response_status,
                self.response_envelope_sha256,
                self.provider_output_sha256,
                self.parsed_proposal_sha256,
                self.admitted_plan_sha256,
            )
            if any(value is None for value in required_admitted_values):
                raise ValueError("an admitted evaluation must bind its complete provenance")
            if self.response_status != "completed":
                raise ValueError("an admitted evaluation must bind a completed response")
            if self.transport_calls != 1:
                raise ValueError("an admitted evaluation must bind exactly one transport call")
        elif self.failure_code is None:
            raise ValueError("a failed evaluation must carry a typed failure code")
        if (
            type(self.validation_checks) is not tuple
            or not self.validation_checks
            or len(self.validation_checks) > 128
        ):
            raise ValueError("validation_checks must be a non-empty tuple")
        if any(
            type(item) is not str or _CODE.fullmatch(item) is None
            for item in self.validation_checks
        ):
            raise ValueError("validation checks must contain bounded closed codes")
        token_values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if any(item is None for item in token_values) and any(
            item is not None for item in token_values
        ):
            raise ValueError("token usage must be entirely present or entirely absent")
        if all(item is not None for item in token_values):
            input_tokens = cast(int, self.input_tokens)
            output_tokens = cast(int, self.output_tokens)
            total_tokens = cast(int, self.total_tokens)
            if any(type(item) is not int or item < 0 for item in token_values):
                raise ValueError("token usage values must be non-negative integers")
            if total_tokens != input_tokens + output_tokens:
                raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        if type(self.transport_calls) is not int or self.transport_calls not in {0, 1}:
            raise ValueError("one policy evaluation may make at most one transport call")
        if self.transport_calls == 0 and any(
            value is not None
            for value in (
                self.returned_model,
                self.response_id,
                self.response_status,
                self.service_tier,
                self.response_envelope_sha256,
                self.provider_output_sha256,
                self.parsed_proposal_sha256,
                self.admitted_plan_sha256,
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
            )
        ):
            raise ValueError("zero transport calls cannot bind response or proposal metadata")
        response_core_bindings = (
            self.returned_model,
            self.response_id,
            self.response_status,
            self.response_envelope_sha256,
            self.provider_output_sha256,
        )
        if any(value is None for value in response_core_bindings) and any(
            value is not None for value in response_core_bindings
        ):
            raise ValueError("response provenance must be entirely present or absent")
        if all(value is None for value in response_core_bindings) and self.service_tier is not None:
            raise ValueError("service_tier cannot exist without response provenance")
        if self.response_status is not None and self.response_status != "completed":
            raise ValueError("bound response provenance must be completed")
        if self.parsed_proposal_sha256 is not None and self.provider_output_sha256 is None:
            raise ValueError("a parsed proposal requires bound provider output")
        if self.admitted_plan_sha256 is not None and self.parsed_proposal_sha256 is None:
            raise ValueError("an admitted plan requires a parsed proposal")
        if self.returned_model is not None and self.transport_calls != 1:
            raise ValueError("response metadata requires one transport call")
        if self.response_envelope_sha256 is not None and self.transport_calls != 1:
            raise ValueError("a response envelope requires one transport call")
        if self.transport_calls == 1 and any(value is None for value in packet_bindings):
            raise ValueError("one transport call requires a complete packet and image binding")
        for latency in (
            self.packet_build_latency_ns,
            self.transport_latency_ns,
            self.parse_latency_ns,
            self.admission_latency_ns,
            self.total_latency_ns,
        ):
            if type(latency) is not int or latency < 0:
                raise ValueError("receipt latency phases must be non-negative integers")
        if (
            self.packet_build_latency_ns
            + self.transport_latency_ns
            + self.parse_latency_ns
            + self.admission_latency_ns
            > self.total_latency_ns
        ):
            raise ValueError("latency phases cannot exceed total latency")
        for count in (
            self.target_count,
            self.decision_count,
            self.keep_count,
            self.drop_count,
            self.replace_count,
            self.keep_uncertain_count,
            self.material_decision_count,
            self.abstain_decision_count,
        ):
            if type(count) is not int or count < 0 or count > 256:
                raise ValueError("receipt census values must be integers in schema bounds")
        if self.decision_count != (
            self.keep_count + self.drop_count + self.replace_count + self.keep_uncertain_count
        ):
            raise ValueError("decision_count must equal the closed operation census")
        if self.decision_count > self.target_count:
            raise ValueError("decision_count cannot exceed target_count")
        if (
            self.evaluation_status is PolicyEvaluationStatus.ADMITTED
            and self.decision_count != self.target_count
        ):
            raise ValueError("an admitted evaluation needs one decision per packet target")
        if self.material_decision_count != self.drop_count + self.replace_count:
            raise ValueError("material decision census must equal DROP plus REPLACE")
        if self.abstain_decision_count != self.keep_uncertain_count:
            raise ValueError("abstain census must equal KEEP_UNCERTAIN")
        if self.evaluation_status is not PolicyEvaluationStatus.ADMITTED:
            if self.admitted_plan_sha256 is not None or any(
                (
                    self.decision_count,
                    self.keep_count,
                    self.drop_count,
                    self.replace_count,
                    self.keep_uncertain_count,
                    self.material_decision_count,
                    self.abstain_decision_count,
                )
            ):
                raise ValueError("a failed evaluation cannot bind an admitted plan or decisions")
        if self.evaluation_status is PolicyEvaluationStatus.EVIDENCE_REJECTED:
            if any(value is not None for value in packet_bindings) or any(
                (
                    self.transport_calls,
                    self.target_count,
                    self.transport_latency_ns,
                    self.parse_latency_ns,
                    self.admission_latency_ns,
                )
            ):
                raise ValueError("evidence rejection must precede packet and transport admission")
        elif self.evaluation_status is PolicyEvaluationStatus.TRANSPORT_ERROR:
            if (
                self.transport_calls != 1
                or any(value is None for value in packet_bindings)
                or any(value is not None for value in response_core_bindings)
                or self.service_tier is not None
                or self.parsed_proposal_sha256 is not None
                or self.parse_latency_ns != 0
                or self.admission_latency_ns != 0
            ):
                raise ValueError("transport error receipt has an invalid stage projection")
        elif self.evaluation_status is PolicyEvaluationStatus.INVALID_RESPONSE:
            if (
                self.transport_calls != 1
                or any(value is None for value in packet_bindings)
                or any(value is None for value in response_core_bindings)
                or self.parsed_proposal_sha256 is not None
                or self.admission_latency_ns != 0
            ):
                raise ValueError("invalid response receipt has an invalid stage projection")
        elif self.evaluation_status is PolicyEvaluationStatus.ADMISSION_REJECTED:
            if (
                self.transport_calls != 1
                or any(value is None for value in packet_bindings)
                or any(value is None for value in response_core_bindings)
                or self.parsed_proposal_sha256 is None
            ):
                raise ValueError("admission rejection receipt has an invalid stage projection")
        for flag in (
            self.external_network_attempted,
            self.model_call_attempted,
            self.local_gpu_used,
            self.mobileworld_action_executed,
            self.evidence_persisted,
            self.screenshot_persisted,
            self.provider_output_persisted,
            self.reasoning_persisted,
        ):
            if type(flag) is not bool:
                raise TypeError("receipt persistence flags must use exact booleans")
        if self.transport_calls == 0 and (
            self.external_network_attempted or self.model_call_attempted
        ):
            raise ValueError("zero transport calls cannot attempt network or model work")
        if self.transport_kind == "FAKE" and (
            self.external_network_attempted or self.model_call_attempted
        ):
            raise ValueError("the fake transport cannot use network or a model")
        if (
            self.transport_kind == "OPENAI_RESPONSES"
            and self.transport_calls == 1
            and (not self.external_network_attempted or not self.model_call_attempted)
        ):
            raise ValueError("an OpenAI transport call must conservatively record both attempts")
        if self.local_gpu_used or self.mobileworld_action_executed:
            raise ValueError("R2.2 policy evaluation cannot use local GPU or execute actions")
        if any(
            (
                self.evidence_persisted,
                self.screenshot_persisted,
                self.provider_output_persisted,
                self.reasoning_persisted,
            )
        ):
            raise ValueError("the R2.2 v1 receipt cannot persist semantic content")

    def to_dict(self) -> dict[str, JsonValue]:
        return r22_policy_receipt_dict(self)

    @property
    def sha256(self) -> str:
        return canonical_sha256(r22_policy_receipt_dict(self))


def r22_policy_receipt_dict(receipt: R22PolicyReceiptV1) -> dict[str, JsonValue]:
    """Project an exact trusted receipt without dispatching a virtual serializer."""

    if type(receipt) is not R22PolicyReceiptV1:
        raise TypeError("receipt projection requires the exact trusted receipt type")
    return {
        "schema_version": receipt.schema_version,
        "logical_call_id": receipt.logical_call_id,
        "host_id": receipt.host_id,
        "packet_id": receipt.packet_id,
        "policy_id": receipt.policy_id,
        "execution_scope": receipt.execution_scope,
        "mode": receipt.mode,
        "requested_model": receipt.requested_model,
        "returned_model": receipt.returned_model,
        "api_method": receipt.api_method,
        "openai_sdk_version": receipt.openai_sdk_version,
        "reasoning_effort": receipt.reasoning_effort,
        "sdk_max_retries": receipt.sdk_max_retries,
        "transport_kind": receipt.transport_kind,
        "transport_authority": receipt.transport_authority,
        "prompt_sha256": receipt.prompt_sha256,
        "output_schema_sha256": receipt.output_schema_sha256,
        "request_config_sha256": receipt.request_config_sha256,
        "evidence_packet_sha256": receipt.evidence_packet_sha256,
        "current_image_sha256": receipt.current_image_sha256,
        "response_id": receipt.response_id,
        "response_status": receipt.response_status,
        "service_tier": receipt.service_tier,
        "response_envelope_sha256": receipt.response_envelope_sha256,
        "provider_output_sha256": receipt.provider_output_sha256,
        "parsed_proposal_sha256": receipt.parsed_proposal_sha256,
        "admitted_plan_sha256": receipt.admitted_plan_sha256,
        "evaluation_status": receipt.evaluation_status.value,
        "failure_code": receipt.failure_code,
        "validation_checks": list(receipt.validation_checks),
        "input_tokens": receipt.input_tokens,
        "output_tokens": receipt.output_tokens,
        "total_tokens": receipt.total_tokens,
        "transport_calls": receipt.transport_calls,
        "packet_build_latency_ns": receipt.packet_build_latency_ns,
        "transport_latency_ns": receipt.transport_latency_ns,
        "parse_latency_ns": receipt.parse_latency_ns,
        "admission_latency_ns": receipt.admission_latency_ns,
        "total_latency_ns": receipt.total_latency_ns,
        "target_count": receipt.target_count,
        "decision_count": receipt.decision_count,
        "keep_count": receipt.keep_count,
        "drop_count": receipt.drop_count,
        "replace_count": receipt.replace_count,
        "keep_uncertain_count": receipt.keep_uncertain_count,
        "material_decision_count": receipt.material_decision_count,
        "abstain_decision_count": receipt.abstain_decision_count,
        "external_network_attempted": receipt.external_network_attempted,
        "model_call_attempted": receipt.model_call_attempted,
        "local_gpu_used": receipt.local_gpu_used,
        "mobileworld_action_executed": receipt.mobileworld_action_executed,
        "evidence_persisted": receipt.evidence_persisted,
        "screenshot_persisted": receipt.screenshot_persisted,
        "provider_output_persisted": receipt.provider_output_persisted,
        "reasoning_persisted": receipt.reasoning_persisted,
    }


def detach_r22_policy_receipt(receipt: R22PolicyReceiptV1) -> R22PolicyReceiptV1:
    """Return a constructor-validated receipt with no caller-owned object aliases."""

    if type(receipt) is not R22PolicyReceiptV1:
        raise TypeError("receipt detachment requires the exact trusted receipt type")
    detached = replace(receipt, validation_checks=tuple(receipt.validation_checks))
    if canonical_sha256(r22_policy_receipt_dict(detached)) != canonical_sha256(
        r22_policy_receipt_dict(receipt)
    ):
        raise RuntimeError("receipt changed while it was being detached")
    return detached


def _receipt_from_canonical_bytes(payload: bytes) -> R22PolicyReceiptV1:
    if type(payload) is not bytes:
        raise TypeError("stored receipt payload must use immutable bytes")
    value = json.loads(payload)
    if type(value) is not dict:
        raise RuntimeError("stored receipt payload is not an object")
    projection = cast(dict[str, Any], value)
    projection["evaluation_status"] = PolicyEvaluationStatus(projection["evaluation_status"])
    checks = projection["validation_checks"]
    if type(checks) is not list:
        raise RuntimeError("stored receipt validation checks are not an array")
    projection["validation_checks"] = tuple(checks)
    receipt = R22PolicyReceiptV1(**projection)
    if canonical_json_bytes(r22_policy_receipt_dict(receipt)) != payload:
        raise RuntimeError("stored receipt payload is not canonical")
    return receipt


@runtime_checkable
class R22PolicyReceiptTransaction(Protocol):
    def prepare(self, receipt: R22PolicyReceiptV1) -> PreparedR22PolicyReceiptPublicationV1: ...

    def abort(self) -> None: ...


@runtime_checkable
class R22PolicyReceiptSink(Protocol):
    def begin(self, logical_call_id: str) -> R22PolicyReceiptTransaction: ...


_PREPARED_PUBLICATION_TOKEN = object()


class PreparedR22PolicyReceiptPublicationV1:
    """Module-owned, canonical receipt publication with a bounded final append."""

    __slots__ = (
        "_canonical_bytes",
        "_lock",
        "_logical_call_id",
        "_published",
        "_receipt_sha256",
        "_transaction",
    )

    def __init__(
        self,
        *,
        transaction: _MemoryPolicyReceiptTransaction,
        logical_call_id: str,
        canonical_bytes: bytes,
        token: object,
    ) -> None:
        if token is not _PREPARED_PUBLICATION_TOKEN:
            raise PermissionError("prepared policy receipt publications are module-owned")
        if type(transaction) is not _MemoryPolicyReceiptTransaction:
            raise TypeError("prepared publication requires the exact memory transaction")
        if type(logical_call_id) is not str or _RUNTIME_ID.fullmatch(logical_call_id) is None:
            raise ValueError("prepared publication logical_call_id is invalid")
        receipt = _receipt_from_canonical_bytes(canonical_bytes)
        if receipt.logical_call_id != logical_call_id:
            raise ValueError("prepared receipt differs from its logical call")
        self._transaction = transaction
        self._logical_call_id = logical_call_id
        self._canonical_bytes = bytes(canonical_bytes)
        self._receipt_sha256 = hashlib.sha256(self._canonical_bytes).hexdigest()
        self._published = False
        self._lock = Lock()

    @property
    def logical_call_id(self) -> str:
        return self._logical_call_id

    @property
    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    @property
    def receipt_sha256(self) -> str:
        return self._receipt_sha256

    @property
    def published(self) -> bool:
        with self._lock:
            return self._published

    def publish(self) -> None:
        """Perform only the prepared sink's short in-memory publication step."""

        if type(self) is not PreparedR22PolicyReceiptPublicationV1:
            raise TypeError("receipt publication requires the exact module-owned type")
        with self._lock:
            if self._published:
                raise RuntimeError("prepared policy receipt was already published")
            self._transaction._publish_prepared(self)
            self._published = True


class _MemoryPolicyReceiptTransaction:
    def __init__(self, sink: MemoryR22PolicyReceiptSink, logical_call_id: str) -> None:
        self._sink = sink
        self._logical_call_id = logical_call_id
        self._finished = False
        self._prepared: PreparedR22PolicyReceiptPublicationV1 | None = None
        self._lock = Lock()

    def prepare(self, receipt: R22PolicyReceiptV1) -> PreparedR22PolicyReceiptPublicationV1:
        with self._lock:
            if self._finished:
                raise RuntimeError("R2.2 policy receipt transaction is already finished")
            if self._prepared is not None:
                raise RuntimeError("R2.2 policy receipt transaction is already prepared")
            if type(receipt) is not R22PolicyReceiptV1:
                raise TypeError("policy receipt must use the exact trusted type")
            if receipt.logical_call_id != self._logical_call_id:
                raise ValueError("receipt logical_call_id differs from its transaction")
            detached = detach_r22_policy_receipt(receipt)
            payload = canonical_json_bytes(r22_policy_receipt_dict(detached))
            prepared = PreparedR22PolicyReceiptPublicationV1(
                transaction=self,
                logical_call_id=self._logical_call_id,
                canonical_bytes=payload,
                token=_PREPARED_PUBLICATION_TOKEN,
            )
            self._prepared = prepared
            return prepared

    def _publish_prepared(self, publication: PreparedR22PolicyReceiptPublicationV1) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError("R2.2 policy receipt transaction is already finished")
            if type(publication) is not PreparedR22PolicyReceiptPublicationV1:
                raise TypeError("receipt publication must use the exact module-owned type")
            if publication is not self._prepared:
                raise ValueError("receipt publication differs from the prepared transaction")
            self._sink._publish_prepared(
                self._logical_call_id,
                publication.canonical_bytes,
            )
            self._finished = True

    def abort(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._sink._abort(self._logical_call_id)
            self._finished = True


class MemoryR22PolicyReceiptSink:
    """Thread-safe transactional sink for CPU tests and embedding hosts."""

    def __init__(self) -> None:
        self._receipt_bytes: list[bytes] = []
        self._active_ids: set[str] = set()
        self._committed_ids: set[str] = set()
        self._lock = Lock()

    @property
    def receipts(self) -> tuple[R22PolicyReceiptV1, ...]:
        with self._lock:
            payloads = tuple(self._receipt_bytes)
        return tuple(_receipt_from_canonical_bytes(payload) for payload in payloads)

    def begin(self, logical_call_id: str) -> _MemoryPolicyReceiptTransaction:
        if type(logical_call_id) is not str or _RUNTIME_ID.fullmatch(logical_call_id) is None:
            raise ValueError("logical_call_id must be a bounded path-safe ID")
        with self._lock:
            if logical_call_id in self._active_ids or logical_call_id in self._committed_ids:
                raise FileExistsError("logical-call policy receipt transaction already exists")
            self._active_ids.add(logical_call_id)
        return _MemoryPolicyReceiptTransaction(self, logical_call_id)

    def _publish_prepared(self, logical_call_id: str, payload: bytes) -> None:
        if type(payload) is not bytes:
            raise TypeError("prepared receipt payload must use immutable bytes")
        if type(self._receipt_bytes) is not list:
            raise RuntimeError("policy receipt storage is not the exact memory sink")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("policy receipt sink is busy at the publication gate")
        try:
            if type(self._receipt_bytes) is not list:
                raise RuntimeError("policy receipt storage changed during publication")
            if logical_call_id not in self._active_ids:
                raise RuntimeError("R2.2 policy receipt transaction is not active")
            set.remove(self._active_ids, logical_call_id)
            set.add(self._committed_ids, logical_call_id)
            list.append(self._receipt_bytes, payload)
        finally:
            self._lock.release()

    def _abort(self, logical_call_id: str) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            set.discard(self._active_ids, logical_call_id)
        finally:
            self._lock.release()


__all__ = [
    "MemoryR22PolicyReceiptSink",
    "PolicyEvaluationStatus",
    "PreparedR22PolicyReceiptPublicationV1",
    "R22PolicyReceiptSink",
    "R22PolicyReceiptTransaction",
    "R22PolicyReceiptV1",
    "R22_POLICY_RECEIPT_SCHEMA_VERSION",
    "detach_r22_policy_receipt",
    "r22_policy_receipt_dict",
]
