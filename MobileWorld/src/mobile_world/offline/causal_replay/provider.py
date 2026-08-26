"""Provider contract helpers; no network-capable implementation is provided in G1.2."""

from __future__ import annotations

import hashlib
import re
from typing import cast

from mobile_world.offline.causal_replay.contracts import (
    AuthorizedProviderRequest,
    HistoryCodecResolver,
    HistoryIR,
    JsonValue,
    PlanSetProfile,
    PortableContractError,
    PreparedProviderRequest,
    ProviderCodec,
    ProviderCodecResolver,
    ProviderResult,
    ProviderResultStatus,
    RawProviderResponse,
    RenderResult,
    TransformationPlan,
    ValidationReceipt,
    canonical_json_bytes,
    canonical_sha256,
)
from mobile_world.offline.causal_replay.core import validate_pre_send

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REF_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$")


def _is_nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_bytes(value: object) -> bool:
    return isinstance(value, bytes)


def validate_validation_receipt_types(receipt: ValidationReceipt) -> None:
    """Reject Python bool/int aliasing before any provider or sidecar decision."""

    for label, value in (
        ("valid", receipt.valid),
        ("provider_invocation_allowed", receipt.provider_invocation_allowed),
        ("invocation_attempted", receipt.invocation_attempted),
    ):
        if not isinstance(value, bool):
            raise PortableContractError(
                "INVALID_VALIDATION_RECEIPT_BOOLEAN",
                f"validation receipt {label} must be an exact boolean",
            )


def authorize_prepared_request(
    prepared: PreparedProviderRequest,
    validation_receipt: ValidationReceipt,
    *,
    ir: HistoryIR,
    plan: TransformationPlan,
    render_result: RenderResult,
    codec_registry: HistoryCodecResolver,
    provider_registry: ProviderCodecResolver,
    codec_contract_version: str,
    paired_plans: tuple[TransformationPlan, ...],
    plan_set_profile: PlanSetProfile,
) -> AuthorizedProviderRequest:
    """Final pure guard called immediately before a future provider transport."""

    validate_validation_receipt_types(validation_receipt)
    if not validation_receipt.valid or not validation_receipt.provider_invocation_allowed:
        raise PortableContractError(
            "PROVIDER_INVOCATION_NOT_AUTHORIZED", "pre-send receipt does not authorize transport"
        )
    if (
        not _is_nonempty_text(prepared.provider_codec_id)
        or not _is_nonempty_text(prepared.provider_contract_version)
        or not _is_nonempty_text(prepared.endpoint_revision)
        or not _is_bytes(prepared.encoded_request)
        or not isinstance(prepared.model_parameters, dict)
    ):
        raise PortableContractError(
            "INVALID_PREPARED_REQUEST", "prepared provider envelope has invalid field types"
        )
    try:
        provider_codec = provider_registry.by_id(
            prepared.provider_codec_id, prepared.provider_contract_version
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise PortableContractError(
            "PROVIDER_CODEC_REGISTRY_RESOLUTION_FAILED",
            "provider codec is not registry-resolved",
        ) from exc
    if (
        not isinstance(cast(object, provider_codec), ProviderCodec)
        or provider_codec.codec_id != prepared.provider_codec_id
        or provider_codec.contract_version != prepared.provider_contract_version
    ):
        raise PortableContractError(
            "PROVIDER_CODEC_REGISTRY_MISMATCH",
            "provider registry returned another codec declaration",
        )
    expected_receipt = validate_pre_send(
        render_result.original_request,
        ir,
        plan,
        render_result,
        codec_registry=codec_registry,
        codec_contract_version=codec_contract_version,
        paired_plans=paired_plans,
        plan_set_profile=plan_set_profile,
        execution_mode=render_result.execution_mode,
        failure_policy=render_result.failure_policy,
        intended_provider_codec_id=prepared.provider_codec_id,
        intended_provider_contract_version=prepared.provider_contract_version,
        intended_endpoint_revision=prepared.endpoint_revision,
        model_parameters=prepared.model_parameters,
    )
    if canonical_sha256(validation_receipt.to_dict()) != canonical_sha256(
        expected_receipt.to_dict()
    ):
        raise PortableContractError(
            "VALIDATION_RECEIPT_MISMATCH",
            "provider guard independently recomputed a different pre-send receipt",
        )
    application_request = render_result.rendered_request
    application_hash = canonical_sha256(application_request)
    if prepared.application_request_sha256 != application_hash:
        raise PortableContractError(
            "PROVIDER_REQUEST_HASH_MISMATCH", "encoded request binds another application request"
        )
    if validation_receipt.rendered_request_sha256 != application_hash:
        raise PortableContractError(
            "VALIDATION_REQUEST_HASH_MISMATCH", "validation receipt binds another request"
        )
    if hashlib.sha256(prepared.encoded_request).hexdigest() != prepared.encoded_request_sha256:
        raise PortableContractError(
            "ENCODED_REQUEST_HASH_MISMATCH", "encoded request bytes do not match their digest"
        )
    parameters_sha = canonical_sha256(prepared.model_parameters)
    if parameters_sha != prepared.model_parameters_sha256:
        raise PortableContractError(
            "MODEL_PARAMETERS_HASH_MISMATCH", "model parameter digest is inconsistent"
        )
    if (
        validation_receipt.intended_provider_codec_id != prepared.provider_codec_id
        or validation_receipt.intended_provider_contract_version
        != prepared.provider_contract_version
        or validation_receipt.intended_endpoint_revision != prepared.endpoint_revision
        or validation_receipt.model_parameters_sha256 != parameters_sha
    ):
        raise PortableContractError(
            "PROVIDER_BINDING_MISMATCH",
            "prepared request differs from the validated provider/endpoint/parameter binding",
        )
    return AuthorizedProviderRequest(
        provider_codec_id=prepared.provider_codec_id,
        provider_contract_version=prepared.provider_contract_version,
        endpoint_revision=prepared.endpoint_revision,
        application_request_sha256=prepared.application_request_sha256,
        encoded_request_sha256=prepared.encoded_request_sha256,
        encoded_request=bytes(prepared.encoded_request),
        model_parameters_json=canonical_json_bytes(prepared.model_parameters),
        model_parameters_sha256=prepared.model_parameters_sha256,
        validation_receipt=validation_receipt,
    )


def validate_provider_result(result: ProviderResult) -> None:
    """Validate pure response/action/hash semantics without performing transport."""

    if (
        not _is_nonempty_text(result.provider_codec_id)
        or not _is_nonempty_text(result.provider_contract_version)
        or not _is_nonempty_text(result.endpoint_revision)
    ):
        raise PortableContractError(
            "PROVIDER_IDENTITY_MISSING", "provider codec and endpoint revision must be non-empty"
        )
    if not isinstance(result.status, ProviderResultStatus):
        raise PortableContractError("INVALID_PROVIDER_STATUS", "provider result status is invalid")
    if not isinstance(result.model_parameters, dict):
        raise PortableContractError(
            "INVALID_MODEL_PARAMETERS", "provider model parameters must be an object"
        )
    _require_sha256(result.application_request_sha256, "application request")
    _require_sha256(result.encoded_request_sha256, "encoded request")
    _require_sha256(result.model_parameters_sha256, "model parameters")
    if canonical_sha256(result.model_parameters) != result.model_parameters_sha256:
        raise PortableContractError(
            "MODEL_PARAMETERS_HASH_MISMATCH", "provider result parameter digest is inconsistent"
        )
    has_response_sha = result.response_sha256 is not None
    has_response_ref = result.raw_response_ref is not None
    if has_response_sha != has_response_ref:
        raise PortableContractError(
            "PROVIDER_RESPONSE_REF_INCOMPLETE",
            "response digest and raw response ref must be present together",
        )
    returned_bytes_required = result.status in {
        ProviderResultStatus.RETURNED,
        ProviderResultStatus.PARSE_ERROR,
    }
    if returned_bytes_required:
        if not has_response_sha or not has_response_ref:
            raise PortableContractError(
                "PROVIDER_RESPONSE_REF_MISSING", "returned response must retain raw bytes"
            )
    if result.status is ProviderResultStatus.MISSING and (has_response_sha or has_response_ref):
        raise PortableContractError(
            "MISSING_RESULT_HAS_RESPONSE", "missing result cannot retain response bytes"
        )
    if has_response_sha and has_response_ref:
        assert result.response_sha256 is not None
        assert result.raw_response_ref is not None
        if not isinstance(result.raw_response_ref, dict):
            raise PortableContractError(
                "INVALID_ARTIFACT_REF", "raw response reference must be an object"
            )
        _require_sha256(result.response_sha256, "provider response")
        _validate_artifact_ref(result.raw_response_ref)
        if result.raw_response_ref.get("sha256") != result.response_sha256:
            raise PortableContractError(
                "PROVIDER_RESPONSE_HASH_MISMATCH", "raw response ref digest is inconsistent"
            )
    if result.status is ProviderResultStatus.RETURNED:
        if (
            not isinstance(result.normalized_action, dict)
            or result.normalized_action_sha256 is None
        ):
            raise PortableContractError(
                "NORMALIZED_ACTION_MISSING", "returned result needs a parsed structured action"
            )
        _require_sha256(result.normalized_action_sha256, "normalized action")
        if canonical_sha256(result.normalized_action) != result.normalized_action_sha256:
            raise PortableContractError(
                "NORMALIZED_ACTION_HASH_MISMATCH", "normalized action digest is inconsistent"
            )
        if result.error is not None:
            raise PortableContractError("RETURNED_RESULT_HAS_ERROR", "returned result has an error")
    else:
        if result.normalized_action is not None or result.normalized_action_sha256 is not None:
            raise PortableContractError(
                "ERROR_RESULT_HAS_ACTION", "error/missing result cannot expose a parsed action"
            )
        if result.error is None:
            raise PortableContractError(
                "ERROR_DETAIL_MISSING", "non-returned result needs an error"
            )
        if not isinstance(result.error, dict):
            raise PortableContractError(
                "INVALID_PROVIDER_ERROR", "provider error must be an object"
            )
        _validate_error(result.error)


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PortableContractError("INVALID_SHA256", f"{label} is not lowercase SHA-256")


def _validate_artifact_ref(ref: dict[str, JsonValue]) -> None:
    expected_keys = {
        "sha256",
        "byte_count",
        "media_type",
        "schema_version",
        "relative_path",
    }
    if set(ref) != expected_keys:
        raise PortableContractError(
            "INVALID_ARTIFACT_REF", "raw response ref has unknown or missing fields"
        )
    sha256 = ref["sha256"]
    byte_count = ref["byte_count"]
    media_type = ref["media_type"]
    schema_version = ref["schema_version"]
    relative_path = ref["relative_path"]
    if not isinstance(sha256, str):
        raise PortableContractError("INVALID_ARTIFACT_REF", "artifact digest must be text")
    _require_sha256(sha256, "raw response artifact")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise PortableContractError(
            "INVALID_ARTIFACT_REF", "artifact byte count must be a non-negative integer"
        )
    if not isinstance(media_type, str) or not media_type:
        raise PortableContractError("INVALID_ARTIFACT_REF", "artifact media type is missing")
    if schema_version is not None and (not isinstance(schema_version, str) or not schema_version):
        raise PortableContractError(
            "INVALID_ARTIFACT_REF", "artifact schema version must be null or non-empty text"
        )
    if (
        not isinstance(relative_path, str)
        or not _SAFE_REF_RE.fullmatch(relative_path)
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
    ):
        raise PortableContractError(
            "INVALID_ARTIFACT_REF", "artifact path must be a safe relative POSIX path"
        )


def _validate_error(error: dict[str, JsonValue]) -> None:
    if set(error) != {"code", "message", "retryable"}:
        raise PortableContractError(
            "INVALID_PROVIDER_ERROR", "provider error has unknown or missing fields"
        )
    if not isinstance(error["code"], str) or not error["code"]:
        raise PortableContractError("INVALID_PROVIDER_ERROR", "provider error code is missing")
    if not isinstance(error["message"], str) or not isinstance(error["retryable"], bool):
        raise PortableContractError(
            "INVALID_PROVIDER_ERROR", "provider error message/retryable types are invalid"
        )


def validate_provider_result_binding(
    result: ProviderResult, authorized: AuthorizedProviderRequest
) -> None:
    validate_provider_result(result)
    prepared = authorized.prepared
    if (
        result.provider_codec_id != prepared.provider_codec_id
        or result.provider_contract_version != prepared.provider_contract_version
        or result.endpoint_revision != prepared.endpoint_revision
        or result.application_request_sha256 != prepared.application_request_sha256
        or result.encoded_request_sha256 != prepared.encoded_request_sha256
        or result.model_parameters_sha256 != prepared.model_parameters_sha256
        or result.model_parameters != prepared.model_parameters
    ):
        raise PortableContractError(
            "PROVIDER_RESULT_BINDING_MISMATCH", "provider result binds another prepared request"
        )


class NoProviderInG12:
    """Sentinel object that makes the ALE-320 no-provider boundary executable."""

    codec_id = "mobileworld.g1.provider.unavailable-in-g1-2/v1"
    contract_version = "v1"

    def encode(
        self, application_request: JsonValue, model_parameters: dict[str, JsonValue]
    ) -> PreparedProviderRequest:
        del application_request, model_parameters
        raise PortableContractError(
            "PROVIDER_NOT_IMPLEMENTED_G1_2", "provider encoding belongs to ALE-322 / G1.4"
        )

    def send(self, authorized: AuthorizedProviderRequest) -> RawProviderResponse:
        del authorized
        raise PortableContractError(
            "PROVIDER_NOT_IMPLEMENTED_G1_2", "provider transport belongs to ALE-322 / G1.4"
        )

    def normalize(
        self, authorized: AuthorizedProviderRequest, response: RawProviderResponse
    ) -> ProviderResult:
        del authorized, response
        raise PortableContractError(
            "PROVIDER_NOT_IMPLEMENTED_G1_2", "response normalization belongs to ALE-322 / G1.4"
        )
