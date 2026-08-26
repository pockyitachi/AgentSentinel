"""Derived G1 sidecar construction after independent pre-send validation."""

from __future__ import annotations

from mobile_world.offline.causal_replay.contracts import (
    AuthorizedProviderRequest,
    HistoryCodecResolver,
    HistoryIR,
    PlanSetProfile,
    PortableContractError,
    ProviderAttemptMetadata,
    ProviderCodecResolver,
    ProviderResult,
    RenderResult,
    ReplaySidecar,
    TransformationPlan,
    ValidationReceipt,
    canonical_sha256,
    flattened_evidence_refs,
    stable_id,
)
from mobile_world.offline.causal_replay.core import (
    render_request,
    restore_original,
    validate_pre_send,
)
from mobile_world.offline.causal_replay.provider import (
    authorize_prepared_request,
    validate_provider_result_binding,
    validate_validation_receipt_types,
)


def build_sidecar(
    *,
    ir: HistoryIR,
    plan: TransformationPlan,
    render_result: RenderResult,
    validation_receipt: ValidationReceipt,
    codec_registry: HistoryCodecResolver,
    codec_contract_version: str,
    paired_plans: tuple[TransformationPlan, ...],
    plan_set_profile: PlanSetProfile,
    provider_registry: ProviderCodecResolver | None = None,
    authorized_request: AuthorizedProviderRequest | None = None,
    provider_result: ProviderResult | None = None,
) -> ReplaySidecar:
    validate_validation_receipt_types(validation_receipt)
    if not validation_receipt.valid:
        raise PortableContractError("INVALID_PRE_SEND_RECEIPT", "sidecar needs a valid receipt")
    prepared = None if authorized_request is None else authorized_request.prepared
    if prepared is None and any(
        value is not None
        for value in (
            validation_receipt.intended_provider_codec_id,
            validation_receipt.intended_provider_contract_version,
            validation_receipt.intended_endpoint_revision,
            validation_receipt.model_parameters_sha256,
        )
    ):
        raise PortableContractError(
            "SIDECAR_PREPARED_REQUEST_MISSING",
            "provider-bound receipt needs the prepared request used to derive it",
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
        intended_provider_codec_id=(None if prepared is None else prepared.provider_codec_id),
        intended_provider_contract_version=(
            None if prepared is None else prepared.provider_contract_version
        ),
        intended_endpoint_revision=(None if prepared is None else prepared.endpoint_revision),
        model_parameters=(None if prepared is None else prepared.model_parameters),
    )
    if canonical_sha256(validation_receipt.to_dict()) != canonical_sha256(
        expected_receipt.to_dict()
    ):
        raise PortableContractError(
            "SIDECAR_VALIDATION_RECEIPT_MISMATCH",
            "sidecar pre-send receipt differs from independent recomputation",
        )
    expected_render = render_request(
        render_result.original_request,
        ir,
        plan,
        execution_mode=render_result.execution_mode,
        failure_policy=render_result.failure_policy,
    )
    if canonical_sha256(expected_render.to_dict()) != canonical_sha256(render_result.to_dict()):
        raise PortableContractError(
            "SIDECAR_RENDER_RECEIPT_MISMATCH", "sidecar render receipt is non-canonical"
        )
    if restore_original(render_result) != render_result.original_request:
        raise PortableContractError(
            "SIDECAR_MAPPING_NOT_REVERSIBLE", "sidecar mapping cannot restore source request"
        )
    if authorized_request is not None:
        if provider_registry is None:
            raise PortableContractError(
                "PROVIDER_CODEC_REGISTRY_MISSING",
                "provider-bound sidecar needs the authoritative provider registry",
            )
        canonical_authorization = authorize_prepared_request(
            authorized_request.prepared,
            validation_receipt,
            ir=ir,
            plan=plan,
            render_result=render_result,
            codec_registry=codec_registry,
            provider_registry=provider_registry,
            codec_contract_version=codec_contract_version,
            paired_plans=paired_plans,
            plan_set_profile=plan_set_profile,
        )
        if authorized_request != canonical_authorization:
            raise PortableContractError(
                "SIDECAR_AUTHORIZATION_MISMATCH", "guarded request is non-canonical"
            )
    if provider_result is not None:
        if authorized_request is None:
            raise PortableContractError(
                "PROVIDER_AUTHORIZATION_MISSING", "provider result needs its guarded request"
            )
        if not validation_receipt.provider_invocation_allowed:
            raise PortableContractError(
                "UNAUTHORIZED_PROVIDER_RESULT", "provider result exists without authorization"
            )
        if provider_result.application_request_sha256 != render_result.rendered_request_sha256:
            raise PortableContractError(
                "SIDECAR_PROVIDER_MISMATCH", "provider result binds another final request"
            )
        validate_provider_result_binding(provider_result, authorized_request)
    evidence = flattened_evidence_refs(plan)
    provider_attempt = ProviderAttemptMetadata(
        invocation_attempted=provider_result is not None,
        provider_codec_id=(None if prepared is None else prepared.provider_codec_id),
        provider_contract_version=(
            None if prepared is None else prepared.provider_contract_version
        ),
        endpoint_revision=(None if prepared is None else prepared.endpoint_revision),
        application_request_sha256=(
            None if prepared is None else prepared.application_request_sha256
        ),
        encoded_request_sha256=(None if prepared is None else prepared.encoded_request_sha256),
        model_parameters_sha256=(None if prepared is None else prepared.model_parameters_sha256),
    )
    sidecar_id = stable_id(
        "sidecar",
        {
            "history_ir_sha256": canonical_sha256(ir.to_dict()),
            "plan_sha256": canonical_sha256(plan.to_dict()),
            "paired_plan_set_sha256": validation_receipt.plan_set_sha256,
            "plan_set_profile": plan_set_profile.value,
            "render_result_sha256": canonical_sha256(render_result.to_dict()),
            "validation_receipt_sha256": canonical_sha256(validation_receipt.to_dict()),
            "provider_attempt_sha256": canonical_sha256(provider_attempt.to_dict()),
            "provider_result_sha256": (
                None if provider_result is None else canonical_sha256(provider_result.to_dict())
            ),
        },
    )
    return ReplaySidecar(
        sidecar_id=sidecar_id,
        history_ir=ir,
        transformation_plan=plan,
        paired_plan_set=paired_plans,
        plan_set_profile=plan_set_profile,
        render_result=render_result,
        validation_receipt=validation_receipt,
        evidence_refs=evidence,
        provider_attempt=provider_attempt,
        provider_result=provider_result,
    )
