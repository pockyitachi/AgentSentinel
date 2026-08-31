"""CPU-only state-frozen exact-request replay state machine."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import cast

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    AuthorizedProviderRequest,
    ExecutionMode,
    FailurePolicy,
    HistoryCodecDeclaration,
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
    RenderResult,
    TransformationPlan,
    ValidationReceipt,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
)
from mobile_world.offline.causal_replay.core import (
    render_request,
    validate_plan_set,
    validate_pre_send,
)
from mobile_world.offline.causal_replay.provider import (
    authorize_prepared_request,
    validate_provider_result_binding,
)
from mobile_world.offline.causal_replay_runner.blinding import (
    BlindingSeal,
    _make_blinded_packet,
    prepare_blinding,
)
from mobile_world.offline.causal_replay_runner.contracts import (
    BLINDED_PACKET_BINDING_SCHEMA_VERSION,
    MAXIMUM_PROVIDER_ATTEMPTS,
    PROTOCOL_VERSION,
    RETRYABLE_FAILURES,
    AttemptEventKind,
    BlindedActionPacket,
    ChunkRecord,
    ExecutionDomain,
    InvarianceReport,
    InvocationPlan,
    LoadedReplayCapsule,
    ProviderExchange,
    ReplayRunnerError,
    ScheduleEntry,
    TerminalAttemptRecord,
    TerminalStatus,
    UnitKind,
)
from mobile_world.offline.causal_replay_runner.invariance import (
    bind_encoded_request,
    verify_invariance,
)
from mobile_world.offline.causal_replay_runner.provider_codec import (
    FAKE_PROVIDER_CODEC_ID,
    FAKE_PROVIDER_ENDPOINT_REVISION,
    PROVIDER_CONTRACT_VERSION,
    ActionParser,
    DeterministicFakeProviderCodec,
    JsonActionParser,
    ProviderTransportFailure,
    final_sdk_arguments,
    normalize_fake_response_pure,
    validate_fake_provider_implementation,
)
from mobile_world.offline.causal_replay_runner.schedule import (
    logical_run_id,
    validate_schedule_block,
    validate_schedule_entry,
)
from mobile_world.offline.causal_replay_runner.store import ReplayArtifactStore


@dataclass(frozen=True)
class PreparedReplayArm:
    invocation_plan: InvocationPlan
    capsule: LoadedReplayCapsule
    schedule: ScheduleEntry
    plan: TransformationPlan
    paired_plans: tuple[TransformationPlan, ...]
    history_ir: HistoryIR
    render_result: RenderResult
    validation_receipt: ValidationReceipt
    invariance_report: InvarianceReport
    final_application_request: dict[str, JsonValue]
    authorized_request: AuthorizedProviderRequest
    history_codec: HistoryCodecDeclaration = field(repr=False, compare=False)
    provider_codec: DeterministicFakeProviderCodec = field(repr=False, compare=False)
    parser: ActionParser = field(repr=False, compare=False)


def _precommit_fake_blinding(
    store: ReplayArtifactStore, prepared: PreparedReplayArm
) -> dict[str, JsonValue]:
    """Persist a fake-only confidential mapping before any response can exist."""

    seal = _fake_blinding_seal(prepared)
    mapping = seal.mapping.to_dict()
    mapping_bytes = canonical_json_bytes(mapping)
    store.write_once(
        f"runs/{prepared.invocation_plan.run_id}/confidential/blinding-map.json",
        mapping_bytes,
    )
    return {
        "blinding_mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
        "key_commitment_sha256": seal.key_commitment_sha256,
        "mapping_persisted_before_response": True,
    }


def _fake_blinding_seal(prepared: PreparedReplayArm) -> BlindingSeal:
    """Derive the deterministic fake-only seal without writing any artifact."""

    run_id = prepared.invocation_plan.run_id
    fake_secret = hashlib.sha256(f"mobileworld-g1-fake-blinding-key-v1|{run_id}".encode()).digest()
    invocation = prepared.invocation_plan
    confidential: set[str] = set()

    def collect(value: JsonValue, *, include_low_entropy: bool) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect(child, include_low_entropy=include_low_entropy)
        elif isinstance(value, list):
            for child in value:
                collect(child, include_low_entropy=include_low_entropy)
        elif isinstance(value, str) and value and (include_low_entropy or len(value) >= 8):
            confidential.add(value)

    # These three roots are identity-only contracts, so even short endpoint,
    # SDK, parser, arm, and version scalars must be denied to the scorer.
    collect(cast(JsonValue, prepared.capsule.public_binding()), include_low_entropy=True)
    collect(cast(JsonValue, prepared.capsule.replay_binding), include_low_entropy=True)
    collect(cast(JsonValue, invocation.to_dict()), include_low_entropy=True)

    # Curated target/correction/history values and receipt/diff identities are
    # sensitive too.  Low-entropy words are not used as deny tokens here: a
    # legitimate unchanged action such as {"type":"click"} must remain
    # representable even when a historical record also contains that word.
    for sensitive_root in (
        {
            "plan_set_sha256": prepared.invocation_plan.plan_set_sha256,
            "plans": [item.to_dict() for item in prepared.paired_plans],
        },
        prepared.history_ir.to_dict(),
        prepared.invariance_report.to_dict(),
        prepared.validation_receipt.to_dict(),
        {
            "diffs": [item.to_dict() for item in prepared.render_result.diffs],
            "list_insertions": [item.to_dict() for item in prepared.render_result.list_insertions],
        },
    ):
        collect(cast(JsonValue, sensitive_root), include_low_entropy=False)

    confidential_values = tuple(sorted(confidential))
    return prepare_blinding(
        run_id=run_id,
        arm=prepared.schedule.arm,
        schedule_id=prepared.schedule.schedule_id,
        secret_key=fake_secret,
        nonce="fake-conformance-pre-response-v1",
        confidential_values=confidential_values,
    )


def _is_stable_reason_code(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is not None


def record_preflight_blocked(
    invocation_plan: InvocationPlan,
    *,
    store: ReplayArtifactStore,
    reason_code: str,
) -> dict[str, JsonValue]:
    """Persist a fail-closed branch once a logical plan identity exists."""

    if (
        invocation_plan.execution_domain is not ExecutionDomain.FAKE_CONFORMANCE
        or not _is_stable_reason_code(reason_code)
    ):
        raise ReplayRunnerError(
            "PREFLIGHT_BLOCK_RECORD_INVALID",
            "blocked preflight needs a fake-domain plan and stable reason code",
        )
    planned_payload: dict[str, JsonValue] = {
        "invocation_plan_sha256": canonical_sha256(invocation_plan.to_dict()),
        "preflight_outcome": "BLOCKED_BEFORE_FAKE_PROVIDER",
    }
    blocked_payload: dict[str, JsonValue] = {
        "reason_code": reason_code,
        "provider_invocation_allowed": False,
        "external_provider_invoked": False,
        "treatment_response_generation_allowed": False,
    }
    existing = store.load_events(invocation_plan.run_id)
    if existing:
        exact_terminal = (
            len(existing) == 2
            and existing[0]["event_kind"] == AttemptEventKind.PLANNED.value
            and _same_json(cast(JsonValue, existing[0]["payload"]), planned_payload)
            and existing[-1]["event_kind"] == AttemptEventKind.PREFLIGHT_BLOCKED.value
            and _same_json(cast(JsonValue, existing[-1]["payload"]), blocked_payload)
        )
        exact_prefix = (
            len(existing) == 1
            and existing[0]["event_kind"] == AttemptEventKind.PLANNED.value
            and _same_json(cast(JsonValue, existing[0]["payload"]), planned_payload)
        )
        if not (exact_terminal or exact_prefix):
            raise ReplayRunnerError(
                "PREFLIGHT_BLOCK_RECORD_COLLISION",
                "logical run already has another append-only event state",
            )
        # Matching ledger bytes must already have their exact plan artifact.
        # Never "repair" a missing artifact before accepting idempotent reuse.
        store.assert_plan_binding(invocation_plan)
        if exact_terminal:
            return cast(dict[str, JsonValue], copy_json(cast(JsonValue, existing[-1])))
    else:
        store.bind_plan(invocation_plan)
        store.append_event(
            run_id=invocation_plan.run_id,
            event_kind=AttemptEventKind.PLANNED,
            provider_attempt_index=None,
            payload=planned_payload,
        )
    blocked = store.append_event(
        run_id=invocation_plan.run_id,
        event_kind=AttemptEventKind.PREFLIGHT_BLOCKED,
        provider_attempt_index=None,
        payload=blocked_payload,
    )
    return blocked.to_dict()


def _record_planned_block_failure(
    plans: tuple[InvocationPlan, ...],
    store: ReplayArtifactStore | None,
    error: PortableContractError | ReplayRunnerError,
) -> None:
    if store is None:
        return
    reason_code = getattr(error, "code", "PREFLIGHT_VALIDATION_FAILED")
    for plan in plans:
        record_preflight_blocked(plan, store=store, reason_code=reason_code)


def _profile_for(unit_kind: UnitKind) -> PlanSetProfile:
    return (
        PlanSetProfile.G1_STRICT_MHR
        if unit_kind is UnitKind.STRICT_MHR
        else PlanSetProfile.G1_CLEAN_CONTROL
    )


def _provider_parameters(
    capsule: LoadedReplayCapsule, *, replay_seed: int, timeout_seconds: int
) -> dict[str, JsonValue]:
    sdk_arguments = cast(
        dict[str, JsonValue], copy_json(cast(JsonValue, capsule.decoding_configuration))
    )
    sdk_arguments["seed"] = replay_seed
    return {
        "sdk_arguments": sdk_arguments,
        "transport": {
            "timeout_seconds": timeout_seconds,
            "sdk_max_retries": 0,
            "maximum_provider_attempts": MAXIMUM_PROVIDER_ATTEMPTS,
            "harness_retryable_failures": [
                "TIMEOUT",
                "HTTP_5XX",
                "CONNECTION_ERROR",
            ],
        },
    }


def _validate_capsule_guards(capsule: LoadedReplayCapsule) -> None:
    for key in (
        "execution_ready",
        "provider_invocation_allowed",
        "treatment_response_generation_allowed",
        "provider_invoked",
    ):
        if (
            type(capsule.source_safety.get(key)) is not bool
            or capsule.source_safety[key] is not False
        ):
            raise ReplayRunnerError(
                "CAPSULE_AUTHORIZATION_GUARD_INVALID",
                f"capsule safety field {key} must remain exact false",
            )
    treatment_count = capsule.source_safety.get("treatment_response_count")
    if treatment_count is not None and (type(treatment_count) is not int or treatment_count != 0):
        raise ReplayRunnerError(
            "CAPSULE_AUTHORIZATION_GUARD_INVALID",
            "capsule treatment response count must remain exact zero",
        )
    restore = capsule.restore_descriptor
    if (
        not isinstance(restore, dict)  # type: ignore[redundant-expr]
        or restore.get("mode") != "SERIALIZED_REQUEST_ONLY"
        or restore.get("external_state_consulted") is not False
        or restore.get("checkpoint_required") is not False
    ):
        raise ReplayRunnerError(
            "LIVE_EXTERNAL_STATE_NOT_RESTORED",
            "CPU G1.4 accepts only serialized-request-only capsules",
        )


def _endpoint_from_capsule(capsule: LoadedReplayCapsule) -> str:
    provider = capsule.replay_binding.get("provider")
    model = capsule.replay_binding.get("model")
    if not isinstance(provider, dict) or not isinstance(model, dict):
        raise ReplayRunnerError("CAPSULE_REPLAY_BINDING_INVALID", "endpoint binding is missing")
    origin = provider.get("endpoint_origin")
    path = provider.get("endpoint_path")
    revision = model.get("revision")
    if not all(isinstance(value, str) and value for value in (origin, path, revision)):
        raise ReplayRunnerError("CAPSULE_REPLAY_BINDING_INVALID", "endpoint identity is incomplete")
    return f"{origin}{path}@{revision}"


def _parser_binding_sha256(
    capsule: LoadedReplayCapsule,
    provider: DeterministicFakeProviderCodec,
    execution_domain: ExecutionDomain,
) -> tuple[str, str]:
    captured_parser_sha = canonical_sha256(capsule.parser_descriptor)
    parser = provider.parser
    binding_id = parser.binding_id
    implementation_sha256 = parser.implementation_sha256
    if (
        not isinstance(binding_id, str)  # type: ignore[redundant-expr]
        or not binding_id
        or not isinstance(implementation_sha256, str)  # type: ignore[redundant-expr]
        or len(implementation_sha256) != 64
        or any(character not in "0123456789abcdef" for character in implementation_sha256)
    ):
        raise ReplayRunnerError(
            "PARSER_DECLARATION_INVALID",
            "the active parser needs an immutable binding and implementation digest",
        )
    return captured_parser_sha, canonical_sha256(
        {
            "execution_domain": execution_domain.value,
            "captured_parser_descriptor_sha256": captured_parser_sha,
            "active_parser_binding_id": binding_id,
            "active_parser_implementation_sha256": implementation_sha256,
        }
    )


class _BoundHistoryCodecResolver:
    def __init__(self, codec: HistoryCodecDeclaration) -> None:
        self._codec = codec

    def by_id(self, codec_id: str, contract_version: str = "v1") -> HistoryCodecDeclaration:
        if codec_id != self._codec.codec_id or contract_version != self._codec.contract_version:
            raise KeyError((codec_id, contract_version))
        return self._codec


class _BoundProviderCodecResolver:
    def __init__(self, codec: ProviderCodec) -> None:
        self._codec = codec

    def by_id(self, codec_id: str, contract_version: str = "v1") -> ProviderCodec:
        if codec_id != self._codec.codec_id or contract_version != self._codec.contract_version:
            raise KeyError((codec_id, contract_version))
        return self._codec


def _report_with_encoded_hash(
    report: InvarianceReport,
    authorized: AuthorizedProviderRequest,
    *,
    final_application_request: dict[str, JsonValue],
) -> InvarianceReport:
    prepared = authorized.prepared
    final_bytes = canonical_json_bytes(final_application_request)
    if final_bytes != prepared.encoded_request:
        raise ReplayRunnerError(
            "FINAL_APPLICATION_REQUEST_MISMATCH",
            "provider bytes differ from independently rebuilt final SDK arguments",
        )
    return bind_encoded_request(
        report,
        encoded_request_sha256=prepared.encoded_request_sha256,
        rendered_application_request_sha256=prepared.application_request_sha256,
        final_application_request_sha256=hashlib.sha256(final_bytes).hexdigest(),
    )


def preflight_block(
    *,
    capsule: LoadedReplayCapsule,
    history_ir: HistoryIR,
    paired_plans: tuple[TransformationPlan, ...],
    schedule_block: tuple[ScheduleEntry, ...],
    history_registry: HistoryCodecResolver,
    provider_registry: ProviderCodecResolver,
    provider_codec_id: str,
    provider_contract_version: str,
    execution_domain: ExecutionDomain,
    code_sha256: str,
    config_sha256: str,
    timeout_seconds: int = 120,
    preflight_store: ReplayArtifactStore | None = None,
) -> tuple[PreparedReplayArm, ...]:
    """Preflight every arm in one pair before any encode/send call is allowed."""

    if execution_domain is not ExecutionDomain.FAKE_CONFORMANCE:
        raise ReplayRunnerError(
            "LIVE_EXECUTION_DEFERRED",
            "live G1 execution needs G1.5/G1.6/G1.7 and separate owner approval",
        )
    resolved_provider = provider_registry.by_id(provider_codec_id, provider_contract_version)
    if type(resolved_provider) is not DeterministicFakeProviderCodec:
        raise ReplayRunnerError(
            "FAKE_PROVIDER_REQUIRED",
            "CPU conformance accepts only the deterministic network-forbidden provider",
        )
    provider = resolved_provider
    if type(provider.parser) is not JsonActionParser:
        raise ReplayRunnerError(
            "EXECUTABLE_PARSER_DEFERRED",
            "CPU fake execution is sealed to the immutable fixture JSON parser; custom adapters are normalize-only",
        )
    validate_fake_provider_implementation(provider)
    if (
        provider.codec_id != FAKE_PROVIDER_CODEC_ID
        or provider.contract_version != PROVIDER_CONTRACT_VERSION
        or provider.endpoint_revision != FAKE_PROVIDER_ENDPOINT_REVISION
    ):
        raise ReplayRunnerError(
            "FAKE_PROVIDER_IDENTITY_INVALID",
            "CPU conformance provider identity and endpoint are frozen",
        )
    if not schedule_block:
        raise ReplayRunnerError("EMPTY_SCHEDULE_BLOCK", "preflight block is empty")
    validate_schedule_block(schedule_block)
    _validate_capsule_guards(capsule)
    first = schedule_block[0]
    if any(
        item.unit_id != capsule.unit_id
        or item.model_id != capsule.model_id
        or item.unit_kind is not capsule.unit_kind
        or item.block_index != first.block_index
        or item.replay_seed != first.replay_seed
        or item.repeat_index != first.repeat_index
        for item in schedule_block
    ):
        raise ReplayRunnerError("SCHEDULE_CAPSULE_MISMATCH", "schedule block binds another pair")
    plan_by_arm = {plan.arm: plan for plan in paired_plans}
    if len(plan_by_arm) != len(paired_plans) or set(plan_by_arm) != {
        item.arm for item in schedule_block
    }:
        raise ReplayRunnerError(
            "PLAN_SET_SCHEDULE_MISMATCH", "paired plans do not exactly cover scheduled arms"
        )
    profile = _profile_for(capsule.unit_kind)
    plan_set_sha = validate_plan_set(
        capsule.semantic_request,
        history_ir,
        paired_plans,
        codec_registry=history_registry,
        codec_contract_version=history_ir.codec_contract_version,
        plan_set_profile=profile,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    parameters = _provider_parameters(
        capsule, replay_seed=first.replay_seed, timeout_seconds=timeout_seconds
    )
    endpoint = provider.endpoint_revision
    captured_parser_sha, parser_sha = _parser_binding_sha256(
        capsule,
        provider,
        execution_domain,
    )
    history_codec = history_registry.by_id(history_ir.codec_id, history_ir.codec_contract_version)
    history_codec_sha = canonical_sha256(history_codec.capabilities.to_dict())
    provider_codec_sha = canonical_sha256(provider.configuration())
    model_binding_sha = canonical_sha256(capsule.replay_binding["model"])
    provider_binding_sha = canonical_sha256(capsule.replay_binding["provider"])
    model_parameters_sha = canonical_sha256(parameters)
    invocation_by_arm: dict[ArmKind, InvocationPlan] = {}
    for schedule in sorted(schedule_block, key=lambda item: item.arm_order_index):
        plan = plan_by_arm[schedule.arm]
        selected_plan_sha = canonical_sha256(plan.to_dict())
        run_id = logical_run_id(
            schedule,
            capsule_body_sha256=capsule.capsule_body_sha256,
            plan_set_sha256=plan_set_sha,
            selected_plan_sha256=selected_plan_sha,
            history_codec_sha256=history_codec_sha,
            provider_codec_sha256=provider_codec_sha,
            parser_binding_sha256=parser_sha,
            model_binding_sha256=model_binding_sha,
            provider_binding_sha256=provider_binding_sha,
            model_parameters_sha256=model_parameters_sha,
            code_sha256=code_sha256,
            config_sha256=config_sha256,
        )
        invocation_by_arm[schedule.arm] = InvocationPlan(
            run_id=run_id,
            execution_domain=execution_domain,
            schedule=schedule,
            capsule_binding=capsule.public_binding(),
            plan_set_sha256=plan_set_sha,
            selected_plan_sha256=selected_plan_sha,
            history_codec_id=history_ir.codec_id,
            history_codec_contract_version=history_ir.codec_contract_version,
            provider_codec_id=provider.codec_id,
            provider_contract_version=provider.contract_version,
            endpoint_revision=endpoint,
            captured_parser_descriptor_sha256=captured_parser_sha,
            parser_binding_sha256=parser_sha,
            model_binding_sha256=model_binding_sha,
            provider_binding_sha256=provider_binding_sha,
            history_codec_sha256=history_codec_sha,
            provider_codec_sha256=provider_codec_sha,
            model_parameters_sha256=model_parameters_sha,
            code_sha256=code_sha256,
            config_sha256=config_sha256,
        )
    planned_invocations = tuple(
        invocation_by_arm[item.arm]
        for item in sorted(schedule_block, key=lambda entry: entry.arm_order_index)
    )
    # Phase 1: independently validate every render and capsule invariant.  No
    # provider encoder or sender is touched until the complete block passes.
    staged: list[
        tuple[ScheduleEntry, TransformationPlan, RenderResult, ValidationReceipt, InvarianceReport]
    ] = []
    for schedule in sorted(schedule_block, key=lambda item: item.arm_order_index):
        plan = plan_by_arm[schedule.arm]
        try:
            render = render_request(
                capsule.semantic_request,
                history_ir,
                plan,
                execution_mode=ExecutionMode.G1_SCIENTIFIC,
                failure_policy=FailurePolicy.BLOCK,
            )
            receipt = validate_pre_send(
                capsule.semantic_request,
                history_ir,
                plan,
                render,
                codec_registry=history_registry,
                codec_contract_version=history_ir.codec_contract_version,
                paired_plans=paired_plans,
                plan_set_profile=profile,
                execution_mode=ExecutionMode.G1_SCIENTIFIC,
                failure_policy=FailurePolicy.BLOCK,
                intended_provider_codec_id=provider.codec_id,
                intended_provider_contract_version=provider.contract_version,
                intended_endpoint_revision=endpoint,
                model_parameters=parameters,
            )
            report = verify_invariance(
                capsule=capsule,
                plan=plan,
                render_result=render,
                validation_receipt=receipt,
            )
            if (
                type(receipt.provider_invocation_allowed) is not bool  # type: ignore[redundant-expr]
                or receipt.provider_invocation_allowed is not True
            ):
                raise ReplayRunnerError(
                    "PREFLIGHT_PROVIDER_AUTHORIZATION_BLOCKED",
                    "the scientific pre-send guard did not authorize the fake-codec request",
                )
        except (PortableContractError, ReplayRunnerError) as exc:
            _record_planned_block_failure(planned_invocations, preflight_store, exc)
            raise
        staged.append((schedule, plan, render, receipt, report))
    # Phase 2: encode and invoke the frozen G1.2 final authorization guard.  A
    # failed Phase 1 therefore proves encode/send/normalize counts remain zero.
    prepared_arms: list[PreparedReplayArm] = []
    for schedule, plan, render, receipt, report in staged:
        invocation = invocation_by_arm[schedule.arm]
        try:
            prepared = provider.encode(render.rendered_request, parameters)
            if prepared.model_parameters_sha256 != invocation.model_parameters_sha256:
                raise ReplayRunnerError(
                    "MODEL_PARAMETERS_BINDING_MISMATCH",
                    "encoded model parameters differ from the planned logical run",
                )
            authorized = authorize_prepared_request(
                prepared,
                receipt,
                ir=history_ir,
                plan=plan,
                render_result=render,
                codec_registry=history_registry,
                provider_registry=provider_registry,
                codec_contract_version=history_ir.codec_contract_version,
                paired_plans=paired_plans,
                plan_set_profile=profile,
            )
            final_application_request = final_sdk_arguments(render.rendered_request, parameters)
            report = _report_with_encoded_hash(
                report,
                authorized,
                final_application_request=final_application_request,
            )
        except (PortableContractError, ReplayRunnerError) as exc:
            _record_planned_block_failure(planned_invocations, preflight_store, exc)
            raise
        prepared_arms.append(
            PreparedReplayArm(
                invocation_plan=invocation,
                capsule=capsule,
                schedule=schedule,
                plan=plan,
                paired_plans=paired_plans,
                history_ir=history_ir,
                render_result=render,
                validation_receipt=receipt,
                invariance_report=report,
                final_application_request=final_application_request,
                authorized_request=authorized,
                history_codec=history_codec,
                provider_codec=provider,
                parser=provider.parser,
            )
        )
    return tuple(prepared_arms)


def _chunk_records(
    metadata: dict[str, JsonValue],
    *,
    response_bytes: bytes,
    store: ReplayArtifactStore,
) -> tuple[ChunkRecord, ...]:
    raw = metadata.get("chunks")
    if not isinstance(raw, list):
        return ()
    records: list[ChunkRecord] = []
    cursor = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ReplayRunnerError("PROVIDER_CHUNK_INVALID", "chunk metadata is invalid")
        byte_count = item.get("byte_count")
        digest = item.get("sha256")
        is_final = item.get("is_final")
        if (
            type(byte_count) is not int
            or byte_count < 0
            or not isinstance(digest, str)
            or type(is_final) is not bool
        ):
            raise ReplayRunnerError("PROVIDER_CHUNK_INVALID", "chunk metadata is invalid")
        chunk = response_bytes[cursor : cursor + byte_count]
        if len(chunk) != byte_count or hashlib.sha256(chunk).hexdigest() != digest:
            raise ReplayRunnerError(
                "PROVIDER_CHUNK_INVALID", "chunk bytes differ from their metadata"
            )
        cursor += byte_count
        records.append(
            ChunkRecord(
                chunk_index=cast(int, item["chunk_index"]),
                byte_count=byte_count,
                sha256=digest,
                is_final=is_final,
                content_ref=store.put_bytes(chunk, media_type="application/octet-stream"),
            )
        )
    if [item.chunk_index for item in records] != list(range(len(records))):
        raise ReplayRunnerError("PROVIDER_CHUNK_INVALID", "chunk order is not contiguous")
    if records and (
        cursor != len(response_bytes)
        or sum(1 for item in records if item.is_final) != 1
        or not records[-1].is_final
    ):
        raise ReplayRunnerError("PROVIDER_CHUNK_INVALID", "stream chunks are incomplete")
    return tuple(records)


def _exchange_for_response(
    prepared: PreparedReplayArm,
    *,
    provider_attempt_index: int,
    response_bytes: bytes | None,
    metadata: dict[str, JsonValue],
    transport_status: str,
    error_code: str | None,
    retryable: bool,
    encoded_request_ref: dict[str, JsonValue],
    raw_response_ref: dict[str, JsonValue] | None,
    chunks: tuple[ChunkRecord, ...] = (),
) -> ProviderExchange:
    request = prepared.authorized_request.prepared
    response_sha = None if response_bytes is None else hashlib.sha256(response_bytes).hexdigest()
    subject: dict[str, JsonValue] = {
        "run_id": prepared.invocation_plan.run_id,
        "provider_attempt_index": provider_attempt_index,
        "encoded_request_sha256": request.encoded_request_sha256,
        "response_sha256": response_sha,
        "transport_status": transport_status,
        "error_code": error_code,
    }
    latency = metadata.get("latency_ms")
    usage = metadata.get("token_usage")
    return ProviderExchange(
        exchange_id=f"g1exchange-{canonical_sha256(subject)[:24]}",
        run_id=prepared.invocation_plan.run_id,
        provider_attempt_index=provider_attempt_index,
        provider_codec_id=request.provider_codec_id,
        provider_contract_version=request.provider_contract_version,
        endpoint_revision=request.endpoint_revision,
        application_request_sha256=request.application_request_sha256,
        final_application_request_sha256=(
            prepared.invariance_report.final_application_request_sha256
        ),
        encoded_request_sha256=request.encoded_request_sha256,
        model_parameters_sha256=request.model_parameters_sha256,
        request_byte_count=len(request.encoded_request),
        encoded_request_ref=encoded_request_ref,
        response_sha256=response_sha,
        response_byte_count=None if response_bytes is None else len(response_bytes),
        raw_response_ref=raw_response_ref,
        chunks=chunks,
        latency_ms=latency if type(latency) is int else None,
        token_usage=(
            cast(dict[str, JsonValue], copy_json(cast(JsonValue, usage)))
            if isinstance(usage, dict)
            else None
        ),
        transport_status=transport_status,
        error_code=error_code,
        retryable=retryable,
        simulated=True,
    )


def _terminal_status(result: ProviderResult) -> TerminalStatus:
    if result.status is ProviderResultStatus.RETURNED:
        action = result.normalized_action or {}
        if action.get("type") in {"wait", "noop", "no_op"} or action.get("action_type") in {
            "wait",
            "noop",
            "no_op",
        }:
            return TerminalStatus.NO_OP
        return TerminalStatus.SUCCESS
    code = None if result.error is None else result.error.get("code")
    if code == "REFUSAL":
        return TerminalStatus.REFUSAL
    if code == "EMPTY_RESPONSE":
        return TerminalStatus.EMPTY_RESPONSE
    if result.status is ProviderResultStatus.PARSE_ERROR:
        return TerminalStatus.PARSE_ERROR
    return TerminalStatus.PROVIDER_ERROR


def _commit_terminal(
    store: ReplayArtifactStore,
    prepared: PreparedReplayArm,
    *,
    status: TerminalStatus,
    attempt_count: int,
    final_event_sha256: str,
    provider_result: ProviderResult | None,
    parser_diagnostics: dict[str, JsonValue],
    retry_reason: str | None,
) -> dict[str, JsonValue]:
    record = TerminalAttemptRecord(
        run_id=prepared.invocation_plan.run_id,
        status=status,
        provider_attempt_count=attempt_count,
        final_event_sha256=final_event_sha256,
        provider_result=provider_result,
        parser_diagnostics=parser_diagnostics,
        retry_reason=retry_reason,
    ).to_dict()
    # A structural terminal envelope is not sufficient proof.  Rehydrate and
    # cross-bind the complete plan/request/exchange/chunk/response/parser chain
    # before any terminal bytes become durable.
    _validate_completed_run_closure(prepared, store, record)
    store._commit_structural_terminal(prepared.invocation_plan.run_id, record)
    return record


def _execution_require(condition: bool, message: str) -> None:
    if not condition:
        raise ReplayRunnerError("PREPARED_ARM_BINDING_MISMATCH", message)


def _same_json(left: JsonValue, right: JsonValue) -> bool:
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _same_authorized_request(
    left: AuthorizedProviderRequest, right: AuthorizedProviderRequest
) -> bool:
    return (
        left.provider_codec_id == right.provider_codec_id
        and left.provider_contract_version == right.provider_contract_version
        and left.endpoint_revision == right.endpoint_revision
        and left.application_request_sha256 == right.application_request_sha256
        and left.encoded_request_sha256 == right.encoded_request_sha256
        and type(left.encoded_request) is bytes
        and left.encoded_request == right.encoded_request
        and type(left.model_parameters_json) is bytes
        and left.model_parameters_json == right.model_parameters_json
        and left.model_parameters_sha256 == right.model_parameters_sha256
        and _same_json(
            left.validation_receipt.to_dict(),
            right.validation_receipt.to_dict(),
        )
    )


def _validate_prepared_arm_for_execution(
    prepared: PreparedReplayArm,
    provider: DeterministicFakeProviderCodec,
) -> None:
    """Rebuild every preflight binding before any run-state write or fake send."""

    invocation = prepared.invocation_plan
    capsule = prepared.capsule
    schedule = prepared.schedule
    _execution_require(
        invocation.execution_domain is ExecutionDomain.FAKE_CONFORMANCE,
        "the invocation plan is not in the fake-conformance domain",
    )
    _validate_capsule_guards(capsule)
    validate_schedule_entry(schedule)
    _execution_require(
        _same_json(invocation.schedule.to_dict(), schedule.to_dict()),
        "the invocation plan binds another schedule entry",
    )
    _execution_require(
        schedule.unit_id == capsule.unit_id
        and schedule.unit_kind is capsule.unit_kind
        and schedule.model_id == capsule.model_id
        and prepared.plan.arm is schedule.arm,
        "the capsule, schedule, and selected arm identities differ",
    )
    _execution_require(
        _same_json(invocation.capsule_binding, capsule.public_binding()),
        "the invocation plan binds another replay capsule",
    )
    _execution_require(
        provider is prepared.provider_codec and provider.parser is prepared.parser,
        "the execution provider or parser is not the preflight-resolved instance",
    )
    _execution_require(
        type(provider) is DeterministicFakeProviderCodec
        and provider.codec_id == FAKE_PROVIDER_CODEC_ID
        and provider.contract_version == PROVIDER_CONTRACT_VERSION
        and provider.endpoint_revision == FAKE_PROVIDER_ENDPOINT_REVISION,
        "the execution provider is not the frozen network-forbidden codec",
    )

    captured_parser_sha, parser_sha = _parser_binding_sha256(
        capsule,
        provider,
        ExecutionDomain.FAKE_CONFORMANCE,
    )
    provider_codec_sha = canonical_sha256(provider.configuration())
    history_codec = prepared.history_codec
    history_codec_sha = canonical_sha256(history_codec.capabilities.to_dict())
    try:
        model_binding_sha = canonical_sha256(capsule.replay_binding["model"])
        provider_binding_sha = canonical_sha256(capsule.replay_binding["provider"])
    except KeyError as exc:
        raise ReplayRunnerError(
            "PREPARED_ARM_BINDING_MISMATCH",
            "the capsule replay binding is incomplete",
        ) from exc
    _execution_require(
        invocation.captured_parser_descriptor_sha256 == captured_parser_sha
        and invocation.parser_binding_sha256 == parser_sha
        and invocation.provider_codec_sha256 == provider_codec_sha
        and invocation.history_codec_sha256 == history_codec_sha
        and invocation.model_binding_sha256 == model_binding_sha
        and invocation.provider_binding_sha256 == provider_binding_sha,
        "codec, parser, model, or provider configuration drifted after preflight",
    )
    _execution_require(
        invocation.history_codec_id == prepared.history_ir.codec_id == history_codec.codec_id
        and invocation.history_codec_contract_version
        == prepared.history_ir.codec_contract_version
        == history_codec.contract_version
        and invocation.provider_codec_id == provider.codec_id
        and invocation.provider_contract_version == provider.contract_version
        and invocation.endpoint_revision == provider.endpoint_revision,
        "the invocation codec identity differs from the executable bindings",
    )

    authorized_prepared = prepared.authorized_request.prepared
    parameters = authorized_prepared.model_parameters
    transport = parameters.get("transport")
    timeout_seconds = transport.get("timeout_seconds") if isinstance(transport, dict) else None
    _execution_require(
        type(timeout_seconds) is int and timeout_seconds > 0,
        "the preflight timeout is not an exact positive integer",
    )
    expected_parameters = _provider_parameters(
        capsule,
        replay_seed=schedule.replay_seed,
        timeout_seconds=cast(int, timeout_seconds),
    )
    _execution_require(
        _same_json(parameters, expected_parameters)
        and invocation.model_parameters_sha256 == canonical_sha256(expected_parameters),
        "the authorized model parameters differ from the frozen replay seed/configuration",
    )

    history_resolver = _BoundHistoryCodecResolver(history_codec)
    provider_resolver = _BoundProviderCodecResolver(provider)
    profile = _profile_for(capsule.unit_kind)
    expected_plan_set_sha = validate_plan_set(
        capsule.semantic_request,
        prepared.history_ir,
        prepared.paired_plans,
        codec_registry=history_resolver,
        codec_contract_version=prepared.history_ir.codec_contract_version,
        plan_set_profile=profile,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    selected_plan_sha = canonical_sha256(prepared.plan.to_dict())
    _execution_require(
        invocation.plan_set_sha256 == expected_plan_set_sha
        and invocation.selected_plan_sha256 == selected_plan_sha,
        "the selected or paired transformation-plan digest differs from preflight",
    )
    matching_plans = tuple(
        plan
        for plan in prepared.paired_plans
        if canonical_sha256(plan.to_dict()) == selected_plan_sha
    )
    _execution_require(
        len(matching_plans) == 1 and matching_plans[0].arm is schedule.arm,
        "the selected plan is not the unique scheduled member of the paired plan set",
    )

    expected_render = render_request(
        capsule.semantic_request,
        prepared.history_ir,
        prepared.plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    expected_receipt = validate_pre_send(
        capsule.semantic_request,
        prepared.history_ir,
        prepared.plan,
        expected_render,
        codec_registry=history_resolver,
        codec_contract_version=prepared.history_ir.codec_contract_version,
        paired_plans=prepared.paired_plans,
        plan_set_profile=profile,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
        intended_provider_codec_id=provider.codec_id,
        intended_provider_contract_version=provider.contract_version,
        intended_endpoint_revision=provider.endpoint_revision,
        model_parameters=expected_parameters,
    )
    _execution_require(
        _same_json(prepared.render_result.to_dict(), expected_render.to_dict())
        and _same_json(prepared.validation_receipt.to_dict(), expected_receipt.to_dict()),
        "the render result or validation receipt is not the canonical preflight result",
    )

    expected_final_request = final_sdk_arguments(
        expected_render.rendered_request,
        expected_parameters,
    )
    expected_encoded = canonical_json_bytes(expected_final_request)
    expected_provider_request = PreparedProviderRequest(
        provider_codec_id=provider.codec_id,
        provider_contract_version=provider.contract_version,
        endpoint_revision=provider.endpoint_revision,
        application_request_sha256=expected_render.rendered_request_sha256,
        encoded_request_sha256=hashlib.sha256(expected_encoded).hexdigest(),
        encoded_request=expected_encoded,
        model_parameters=expected_parameters,
        model_parameters_sha256=canonical_sha256(expected_parameters),
    )
    expected_authorized = authorize_prepared_request(
        expected_provider_request,
        expected_receipt,
        ir=prepared.history_ir,
        plan=prepared.plan,
        render_result=expected_render,
        codec_registry=history_resolver,
        provider_registry=provider_resolver,
        codec_contract_version=prepared.history_ir.codec_contract_version,
        paired_plans=prepared.paired_plans,
        plan_set_profile=profile,
    )
    expected_report = _report_with_encoded_hash(
        verify_invariance(
            capsule=capsule,
            plan=prepared.plan,
            render_result=expected_render,
            validation_receipt=expected_receipt,
        ),
        expected_authorized,
        final_application_request=expected_final_request,
    )
    _execution_require(
        _same_json(prepared.final_application_request, expected_final_request)
        and _same_authorized_request(prepared.authorized_request, expected_authorized)
        and _same_json(prepared.invariance_report.to_dict(), expected_report.to_dict()),
        "the authorized bytes, final SDK arguments, or invariance receipt were spliced",
    )

    expected_run_id = logical_run_id(
        schedule,
        capsule_body_sha256=capsule.capsule_body_sha256,
        plan_set_sha256=expected_plan_set_sha,
        selected_plan_sha256=selected_plan_sha,
        history_codec_sha256=history_codec_sha,
        provider_codec_sha256=provider_codec_sha,
        parser_binding_sha256=parser_sha,
        model_binding_sha256=model_binding_sha,
        provider_binding_sha256=provider_binding_sha,
        model_parameters_sha256=canonical_sha256(expected_parameters),
        code_sha256=invocation.code_sha256,
        config_sha256=invocation.config_sha256,
    )
    expected_invocation = InvocationPlan(
        run_id=expected_run_id,
        execution_domain=ExecutionDomain.FAKE_CONFORMANCE,
        schedule=schedule,
        capsule_binding=capsule.public_binding(),
        plan_set_sha256=expected_plan_set_sha,
        selected_plan_sha256=selected_plan_sha,
        history_codec_id=prepared.history_ir.codec_id,
        history_codec_contract_version=prepared.history_ir.codec_contract_version,
        provider_codec_id=provider.codec_id,
        provider_contract_version=provider.contract_version,
        endpoint_revision=provider.endpoint_revision,
        captured_parser_descriptor_sha256=captured_parser_sha,
        parser_binding_sha256=parser_sha,
        model_binding_sha256=model_binding_sha,
        provider_binding_sha256=provider_binding_sha,
        history_codec_sha256=history_codec_sha,
        provider_codec_sha256=provider_codec_sha,
        model_parameters_sha256=canonical_sha256(expected_parameters),
        code_sha256=invocation.code_sha256,
        config_sha256=invocation.config_sha256,
    )
    if not _same_json(invocation.to_dict(), expected_invocation.to_dict()):
        raise ReplayRunnerError(
            "INVOCATION_PLAN_BINDING_MISMATCH",
            "the logical run ID or invocation-plan closure was changed after preflight",
        )


def _expected_artifact_ref(data: bytes, *, media_type: str) -> dict[str, JsonValue]:
    digest = hashlib.sha256(data).hexdigest()
    return {
        "relative_path": f"objects/sha256/{digest[:2]}/{digest}",
        "sha256": digest,
        "byte_count": len(data),
        "media_type": media_type,
    }


def _rehydrate_expected_artifact(
    store: ReplayArtifactStore,
    value: object,
    expected_bytes: bytes,
    *,
    media_type: str,
) -> dict[str, JsonValue]:
    _execution_require(isinstance(value, dict), "attempt artifact reference is not an object")
    ref = cast(dict[str, JsonValue], value)
    expected_ref = _expected_artifact_ref(expected_bytes, media_type=media_type)
    _execution_require(
        _same_json(ref, expected_ref),
        "attempt artifact reference differs from its content-addressed binding",
    )
    _execution_require(
        store.read_artifact_ref(ref) == expected_bytes,
        "attempt artifact bytes differ from the prepared replay arm",
    )
    return ref


def _canonical_object_from_ref(
    store: ReplayArtifactStore, value: object
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    _execution_require(isinstance(value, dict), "JSON artifact reference is not an object")
    ref = cast(dict[str, JsonValue], value)
    data = store.read_artifact_ref(ref)
    try:
        decoded = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayRunnerError(
            "ATTEMPT_ARTIFACT_CLOSURE_INVALID",
            "attempt JSON artifact cannot be decoded",
        ) from exc
    _execution_require(
        isinstance(decoded, dict) and canonical_json_bytes(cast(JsonValue, decoded)) == data,
        "attempt JSON artifact is not a canonical object",
    )
    return cast(dict[str, JsonValue], decoded), ref


def _terminal_provider_result(value: dict[str, JsonValue]) -> ProviderResult:
    return ProviderResult(
        provider_codec_id=cast(str, value["provider_codec_id"]),
        provider_contract_version=cast(str, value["provider_contract_version"]),
        endpoint_revision=cast(str, value["endpoint_revision"]),
        status=ProviderResultStatus(cast(str, value["status"])),
        application_request_sha256=cast(str, value["application_request_sha256"]),
        encoded_request_sha256=cast(str, value["encoded_request_sha256"]),
        response_sha256=cast(str | None, value["response_sha256"]),
        raw_response_ref=cast(dict[str, JsonValue] | None, value["raw_response_ref"]),
        normalized_action=cast(dict[str, JsonValue] | None, value["normalized_action"]),
        normalized_action_sha256=cast(str | None, value["normalized_action_sha256"]),
        error=cast(dict[str, JsonValue] | None, value["error"]),
        model_parameters=cast(dict[str, JsonValue], value["model_parameters"]),
        model_parameters_sha256=cast(str, value["model_parameters_sha256"]),
    )


def _validate_completed_run_closure(
    prepared: PreparedReplayArm,
    store: ReplayArtifactStore,
    terminal_record: dict[str, JsonValue],
) -> None:
    """Rehydrate every required derived artifact before idempotent terminal reuse."""

    run_id = prepared.invocation_plan.run_id
    events = store.load_events(run_id)
    _execution_require(
        len(events) >= 5
        and events[0]["event_kind"] == AttemptEventKind.PLANNED.value
        and events[1]["event_kind"] == AttemptEventKind.PREFLIGHT_ALLOWED.value
        and events[-1]["event_kind"] == AttemptEventKind.TERMINAL.value,
        "completed run is missing its planned, allowed, or terminal event",
    )

    plan_bytes = canonical_json_bytes(prepared.plan.to_dict())
    paired_bytes = canonical_json_bytes(
        {
            "plan_set_sha256": prepared.invocation_plan.plan_set_sha256,
            "plans": [item.to_dict() for item in prepared.paired_plans],
        }
    )
    invariance_bytes = canonical_json_bytes(prepared.invariance_report.to_dict())
    render_bytes = canonical_json_bytes(prepared.render_result.to_dict())
    receipt_bytes = canonical_json_bytes(prepared.validation_receipt.to_dict())
    final_request_bytes = canonical_json_bytes(prepared.final_application_request)
    diff_bytes = canonical_json_bytes(
        {
            "diffs": [item.to_dict() for item in prepared.render_result.diffs],
            "list_insertions": [item.to_dict() for item in prepared.render_result.list_insertions],
        }
    )
    planned_payload = cast(dict[str, JsonValue], events[0]["payload"])
    expected_planned_keys = {
        "invocation_plan_sha256",
        "selected_plan_ref",
        "paired_plan_set_ref",
        "invariance_report_ref",
        "render_result_ref",
        "validation_receipt_ref",
        "final_application_request_ref",
        "target_diff_ref",
        "blinding_commitment",
    }
    _execution_require(
        set(planned_payload) == expected_planned_keys
        and planned_payload.get("invocation_plan_sha256")
        == canonical_sha256(prepared.invocation_plan.to_dict()),
        "PLANNED does not bind the exact invocation plan and required artifact set",
    )
    for key, expected_bytes in (
        ("selected_plan_ref", plan_bytes),
        ("paired_plan_set_ref", paired_bytes),
        ("invariance_report_ref", invariance_bytes),
        ("render_result_ref", render_bytes),
        ("validation_receipt_ref", receipt_bytes),
        ("final_application_request_ref", final_request_bytes),
        ("target_diff_ref", diff_bytes),
    ):
        _rehydrate_expected_artifact(
            store,
            planned_payload.get(key),
            expected_bytes,
            media_type="application/json",
        )

    seal = _fake_blinding_seal(prepared)
    mapping_bytes = canonical_json_bytes(seal.mapping.to_dict())
    _execution_require(
        store.read_logical(f"runs/{run_id}/confidential/blinding-map.json") == mapping_bytes,
        "confidential blinding mapping is missing or differs from the pre-response seal",
    )
    _execution_require(
        _same_json(
            planned_payload.get("blinding_commitment"),
            {
                "blinding_mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
                "key_commitment_sha256": seal.key_commitment_sha256,
                "mapping_persisted_before_response": True,
            },
        ),
        "public blinding commitment is incomplete or exposes another mapping",
    )

    allowed_payload = cast(dict[str, JsonValue], events[1]["payload"])
    request_ref = _rehydrate_expected_artifact(
        store,
        allowed_payload.get("encoded_request_ref"),
        prepared.authorized_request.encoded_request,
        media_type="application/json",
    )
    _execution_require(
        set(allowed_payload)
        == {
            "fake_conformance",
            "external_provider_invocation_allowed",
            "encoded_request_ref",
        }
        and allowed_payload.get("fake_conformance") is True
        and allowed_payload.get("external_provider_invocation_allowed") is False,
        "PREFLIGHT_ALLOWED is not the exact network-forbidden fake decision",
    )

    position = 2
    expected_attempt = 1
    while position < len(events):
        started = events[position]
        _execution_require(
            started["event_kind"] == AttemptEventKind.ATTEMPT_STARTED.value
            and started["provider_attempt_index"] == expected_attempt
            and _same_json(
                cast(JsonValue, started["payload"]),
                {
                    "encoded_request_sha256": prepared.authorized_request.encoded_request_sha256,
                    "simulated": True,
                    "external_provider_invoked": False,
                },
            ),
            "provider attempt start differs from the preflight-authorized bytes",
        )
        position += 1
        chunks: list[ChunkRecord] = []
        while (
            position < len(events)
            and events[position]["event_kind"] == AttemptEventKind.CHUNK.value
        ):
            event = events[position]
            payload = cast(dict[str, JsonValue], event["payload"])
            _execution_require(
                event["provider_attempt_index"] == expected_attempt
                and set(payload)
                == {"chunk_index", "byte_count", "sha256", "is_final", "content_ref"}
                and type(payload.get("chunk_index")) is int
                and payload.get("chunk_index") == len(chunks)
                and type(payload.get("byte_count")) is int
                and cast(int, payload["byte_count"]) >= 0
                and isinstance(payload.get("sha256"), str)
                and type(payload.get("is_final")) is bool,
                "stream chunk envelope is invalid or out of order",
            )
            ref = cast(dict[str, JsonValue], payload["content_ref"])
            chunk_bytes = store.read_artifact_ref(ref)
            _execution_require(
                _same_json(
                    ref,
                    _expected_artifact_ref(
                        chunk_bytes,
                        media_type="application/octet-stream",
                    ),
                )
                and len(chunk_bytes) == payload["byte_count"]
                and hashlib.sha256(chunk_bytes).hexdigest() == payload["sha256"],
                "stream chunk content reference is missing or inconsistent",
            )
            chunks.append(
                ChunkRecord(
                    chunk_index=cast(int, payload["chunk_index"]),
                    byte_count=cast(int, payload["byte_count"]),
                    sha256=cast(str, payload["sha256"]),
                    is_final=cast(bool, payload["is_final"]),
                    content_ref=ref,
                )
            )
            position += 1
        _execution_require(position < len(events), "provider attempt has no outcome")
        outcome = events[position]
        payload = cast(dict[str, JsonValue], outcome["payload"])
        if outcome["event_kind"] == AttemptEventKind.FAILED.value:
            _execution_require(
                outcome["provider_attempt_index"] == expected_attempt
                and set(payload) == {"error_code", "retryable", "exchange_ref"}
                and payload.get("error_code") in RETRYABLE_FAILURES
                and payload.get("retryable") is True
                and all(not item.is_final for item in chunks),
                "failed attempt is not a frozen retryable fake transport outcome",
            )
            exchange_value, exchange_ref = _canonical_object_from_ref(
                store, payload.get("exchange_ref")
            )
            expected_exchange = _exchange_for_response(
                prepared,
                provider_attempt_index=expected_attempt,
                response_bytes=None,
                metadata={},
                transport_status="FAILED",
                error_code=cast(str, payload["error_code"]),
                retryable=True,
                encoded_request_ref=request_ref,
                raw_response_ref=None,
                chunks=tuple(chunks),
            ).to_dict()
            _execution_require(
                _same_json(exchange_value, expected_exchange)
                and _same_json(
                    exchange_ref,
                    _expected_artifact_ref(
                        canonical_json_bytes(expected_exchange),
                        media_type="application/json",
                    ),
                ),
                "failed provider exchange is missing or cross-bound",
            )
            position += 1
            if (
                position < len(events)
                and events[position]["event_kind"] == AttemptEventKind.TERMINAL.value
            ):
                terminal = events[position]
                expected_terminal_payload: dict[str, JsonValue] = {
                    "terminal_status": TerminalStatus.RETRY_EXHAUSTED.value,
                    "retry_reason": cast(str, payload["error_code"]),
                    "preceding_event_sha256": canonical_sha256(outcome),
                }
                _execution_require(
                    expected_attempt == MAXIMUM_PROVIDER_ATTEMPTS
                    and terminal["provider_attempt_index"] == expected_attempt
                    and _same_json(cast(JsonValue, terminal["payload"]), expected_terminal_payload)
                    and terminal_record.get("status") == TerminalStatus.RETRY_EXHAUSTED.value
                    and terminal_record.get("provider_attempt_count") == expected_attempt
                    and terminal_record.get("provider_result") is None
                    and terminal_record.get("retry_reason") == payload["error_code"]
                    and _same_json(
                        terminal_record.get("parser_diagnostics"),
                        {"parse_outcome": "NOT_RUN"},
                    ),
                    "retry-exhausted terminal closure is inconsistent",
                )
                _execution_require(
                    terminal_record.get("final_event_sha256") == canonical_sha256(terminal)
                    and position + 1 == len(events),
                    "retry-exhausted terminal event is not the final ledger record",
                )
                return
            expected_attempt += 1
            continue

        _execution_require(
            outcome["event_kind"] == AttemptEventKind.RETURNED.value
            and outcome["provider_attempt_index"] == expected_attempt
            and set(payload) == {"response_ref", "exchange_ref"},
            "provider return event is invalid",
        )
        response_ref_value = payload.get("response_ref")
        _execution_require(isinstance(response_ref_value, dict), "response ref is missing")
        response_ref = cast(dict[str, JsonValue], response_ref_value)
        response_bytes = store.read_artifact_ref(response_ref)
        _execution_require(
            _same_json(
                response_ref,
                _expected_artifact_ref(
                    response_bytes,
                    media_type="application/octet-stream",
                ),
            ),
            "returned response content address is inconsistent",
        )
        if chunks:
            _execution_require(
                b"".join(store.read_artifact_ref(item.content_ref) for item in chunks)
                == response_bytes
                and sum(1 for item in chunks if item.is_final) == 1
                and chunks[-1].is_final,
                "streaming chunks do not close to the returned response",
            )
        exchange_value, exchange_ref = _canonical_object_from_ref(
            store, payload.get("exchange_ref")
        )
        latency_ms = exchange_value.get("latency_ms")
        token_usage = exchange_value.get("token_usage")
        _execution_require(
            type(latency_ms) is int
            and latency_ms >= 0
            and isinstance(token_usage, dict)
            and set(token_usage) == {"input_tokens", "output_tokens"}
            and all(
                type(token_usage.get(key)) is int and cast(int, token_usage[key]) >= 0
                for key in ("input_tokens", "output_tokens")
            ),
            "returned provider telemetry is not the closed nonnegative fake receipt",
        )
        metadata: dict[str, JsonValue] = {
            "latency_ms": cast(int, latency_ms),
            "token_usage": cast(dict[str, JsonValue], token_usage),
        }
        expected_exchange = _exchange_for_response(
            prepared,
            provider_attempt_index=expected_attempt,
            response_bytes=response_bytes,
            metadata=metadata,
            transport_status="RETURNED",
            error_code=None,
            retryable=False,
            encoded_request_ref=request_ref,
            raw_response_ref=response_ref,
            chunks=tuple(chunks),
        ).to_dict()
        _execution_require(
            _same_json(exchange_value, expected_exchange)
            and _same_json(
                exchange_ref,
                _expected_artifact_ref(
                    canonical_json_bytes(expected_exchange),
                    media_type="application/json",
                ),
            ),
            "returned provider exchange is missing or cross-bound",
        )
        position += 1
        _execution_require(position + 1 < len(events), "parsed and terminal records are missing")
        parsed = events[position]
        terminal = events[position + 1]
        result_value = terminal_record.get("provider_result")
        _execution_require(isinstance(result_value, dict), "terminal provider result is missing")
        result = _terminal_provider_result(cast(dict[str, JsonValue], result_value))
        validate_provider_result_binding(result, prepared.authorized_request)
        _execution_require(
            result.response_sha256 == hashlib.sha256(response_bytes).hexdigest()
            and isinstance(result.raw_response_ref, dict)
            and store.read_artifact_ref(result.raw_response_ref) == response_bytes
            and _same_json(
                result.raw_response_ref,
                {
                    **_expected_artifact_ref(
                        response_bytes,
                        media_type="application/octet-stream",
                    ),
                    "schema_version": None,
                },
            ),
            "terminal provider result does not bind the returned raw response",
        )
        expected_parse_kind = (
            AttemptEventKind.PARSED
            if result.status is ProviderResultStatus.RETURNED
            else AttemptEventKind.PARSE_FAILED
        )
        parser_diagnostics = terminal_record.get("parser_diagnostics")
        expected_result, expected_parser_diagnostics = normalize_fake_response_pure(
            prepared.authorized_request,
            response_bytes,
        )
        expected_result = replace(
            expected_result,
            raw_response_ref={**response_ref, "schema_version": None},
        )
        _execution_require(
            _same_json(result.to_dict(), expected_result.to_dict())
            and _same_json(parser_diagnostics, expected_parser_diagnostics)
            and parsed["event_kind"] == expected_parse_kind.value
            and parsed["provider_attempt_index"] == expected_attempt
            and _same_json(
                cast(JsonValue, parsed["payload"]),
                {
                    "provider_result_sha256": canonical_sha256(result.to_dict()),
                    "parser_diagnostics": parser_diagnostics,
                },
            ),
            "parser event and terminal provider result are cross-bound",
        )
        expected_status = _terminal_status(result)
        _execution_require(
            terminal["event_kind"] == AttemptEventKind.TERMINAL.value
            and terminal["provider_attempt_index"] == expected_attempt
            and _same_json(
                cast(JsonValue, terminal["payload"]),
                {
                    "terminal_status": expected_status.value,
                    "retry_reason": None,
                    "preceding_event_sha256": canonical_sha256(parsed),
                    "generated_action_executed": False,
                },
            )
            and terminal_record.get("status") == expected_status.value
            and terminal_record.get("provider_attempt_count") == expected_attempt
            and terminal_record.get("retry_reason") is None
            and terminal_record.get("final_event_sha256") == canonical_sha256(terminal)
            and position + 2 == len(events),
            "terminal receipt does not close the final parser event",
        )
        return
    raise ReplayRunnerError(
        "ATTEMPT_ARTIFACT_CLOSURE_INVALID",
        "completed run ledger has no validated terminal closure",
    )


def _read_completed_terminal(
    prepared: PreparedReplayArm,
    store: ReplayArtifactStore,
) -> dict[str, JsonValue] | None:
    """Return a terminal only after the full runner-owned closure validates."""

    terminal_value = store._read_structural_terminal(prepared.invocation_plan.run_id)
    if terminal_value is None:
        return None
    store.assert_plan_binding(prepared.invocation_plan)
    terminal = cast(dict[str, JsonValue], terminal_value)
    _validate_completed_run_closure(prepared, store, terminal)
    return terminal


def build_blinded_packet(
    prepared: PreparedReplayArm,
    *,
    store: ReplayArtifactStore,
) -> BlindedActionPacket:
    """Build one scorer packet only from an exact, fully closed fake terminal."""

    provider = prepared.provider_codec
    validate_fake_provider_implementation(provider)
    _validate_prepared_arm_for_execution(prepared, provider)
    terminal_value = _read_completed_terminal(prepared, store)
    if terminal_value is None:
        raise ReplayRunnerError(
            "BLINDED_EXPORT_TERMINAL_REQUIRED",
            "a fully committed and validated terminal is required before scorer export",
        )
    terminal = terminal_value

    result_value = terminal.get("provider_result")
    normalized_action: dict[str, JsonValue] | None = None
    normalized_action_sha256: str | None = None
    if isinstance(result_value, dict):
        action_value = result_value.get("normalized_action")
        if action_value is not None:
            _execution_require(
                isinstance(action_value, dict),
                "terminal normalized action is not an object",
            )
            normalized_action = cast(dict[str, JsonValue], copy_json(cast(JsonValue, action_value)))
            normalized_action_sha256 = canonical_sha256(normalized_action)
            _execution_require(
                normalized_action_sha256 == result_value.get("normalized_action_sha256"),
                "terminal normalized action digest is inconsistent",
            )
    diagnostics_value = terminal.get("parser_diagnostics")
    _execution_require(
        isinstance(diagnostics_value, dict),
        "terminal parser diagnostics are not an object",
    )
    terminal_diagnostics = cast(dict[str, JsonValue], copy_json(diagnostics_value))
    public_diagnostics = cast(dict[str, JsonValue], copy_json(diagnostics_value))
    parser_outcome = public_diagnostics.get("parse_outcome")
    _execution_require(
        isinstance(parser_outcome, str),
        "terminal parser outcome is missing",
    )
    if parser_outcome == "FAILED":
        parser_outcome = "PARSE_ERROR"
        public_diagnostics["parse_outcome"] = parser_outcome

    seal = _fake_blinding_seal(prepared)
    packet = _make_blinded_packet(
        seal=seal,
        normalized_action=normalized_action,
        parser_outcome=cast(str, parser_outcome),
        parser_diagnostics=public_diagnostics,
    )
    packet_bytes = canonical_json_bytes(packet.to_dict())
    mapping_bytes = canonical_json_bytes(seal.mapping.to_dict())
    binding: dict[str, JsonValue] = {
        "schema_version": BLINDED_PACKET_BINDING_SCHEMA_VERSION,
        "record_type": "g1_confidential_blinded_packet_binding",
        "protocol_version": PROTOCOL_VERSION,
        "blinded_packet_id": packet.blinded_packet_id,
        "run_id": prepared.invocation_plan.run_id,
        "terminal_final_event_sha256": cast(str, terminal["final_event_sha256"]),
        "normalized_action_sha256": normalized_action_sha256,
        "terminal_diagnostics_sha256": canonical_sha256(terminal_diagnostics),
        "parser_diagnostics_sha256": canonical_sha256(packet.parser_diagnostics),
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
        "source_artifacts_valid": True,
        "scorer_visible": False,
    }
    run_id = prepared.invocation_plan.run_id
    binding_path = f"runs/{run_id}/confidential/blinded-packet-binding.json"
    packet_path = f"scorer/packets/{packet.blinded_packet_id}.json"
    binding_bytes = canonical_json_bytes(binding)
    store.assert_write_compatible(binding_path, binding_bytes)
    store.assert_write_compatible(packet_path, packet_bytes)
    # Public packet first: a pre-existing collision cannot leave a new
    # confidential receipt claiming a packet that was never emitted.
    store.write_once(packet_path, packet_bytes)
    store.write_once(
        binding_path,
        binding_bytes,
    )
    return packet


def execute_fake_arm(
    prepared: PreparedReplayArm,
    *,
    provider_registry: ProviderCodecResolver,
    store: ReplayArtifactStore,
) -> dict[str, JsonValue]:
    """Execute a deterministic fake attempt; never contacts an external provider."""

    if prepared.invocation_plan.execution_domain is not ExecutionDomain.FAKE_CONFORMANCE:
        raise ReplayRunnerError("LIVE_EXECUTION_DEFERRED", "only fake conformance is executable")
    resolved_provider = provider_registry.by_id(
        prepared.invocation_plan.provider_codec_id,
        prepared.invocation_plan.provider_contract_version,
    )
    if type(resolved_provider) is not DeterministicFakeProviderCodec:
        raise ReplayRunnerError("FAKE_PROVIDER_REQUIRED", "external provider is forbidden")
    provider = resolved_provider
    validate_fake_provider_implementation(provider)
    _validate_prepared_arm_for_execution(prepared, provider)
    existing = _read_completed_terminal(prepared, store)
    if existing is not None:
        return cast(
            dict[str, JsonValue],
            {
                **existing,
                "idempotent_reuse": True,
            },
        )
    store._assert_no_ambiguous_delivery(prepared.invocation_plan.run_id)
    store.bind_plan(prepared.invocation_plan)
    blinding_commitment = _precommit_fake_blinding(store, prepared)
    provider.begin_run(prepared.invocation_plan.run_id)
    selected_plan_ref = store.put_json(prepared.plan.to_dict())
    paired_plan_set_ref = store.put_json(
        {
            "plan_set_sha256": prepared.invocation_plan.plan_set_sha256,
            "plans": [item.to_dict() for item in prepared.paired_plans],
        }
    )
    invariance_ref = store.put_json(prepared.invariance_report.to_dict())
    render_result_ref = store.put_json(prepared.render_result.to_dict())
    validation_receipt_ref = store.put_json(prepared.validation_receipt.to_dict())
    final_application_request_ref = store.put_json(prepared.final_application_request)
    target_diff_ref = store.put_json(
        {
            "diffs": [item.to_dict() for item in prepared.render_result.diffs],
            "list_insertions": [item.to_dict() for item in prepared.render_result.list_insertions],
        }
    )
    request_ref = store.put_bytes(
        prepared.authorized_request.encoded_request,
        media_type="application/json",
    )
    store.ensure_preflight_event(
        run_id=prepared.invocation_plan.run_id,
        event_kind=AttemptEventKind.PLANNED,
        payload={
            "invocation_plan_sha256": canonical_sha256(prepared.invocation_plan.to_dict()),
            "selected_plan_ref": selected_plan_ref,
            "paired_plan_set_ref": paired_plan_set_ref,
            "invariance_report_ref": invariance_ref,
            "render_result_ref": render_result_ref,
            "validation_receipt_ref": validation_receipt_ref,
            "final_application_request_ref": final_application_request_ref,
            "target_diff_ref": target_diff_ref,
            "blinding_commitment": blinding_commitment,
        },
    )
    store.ensure_preflight_event(
        run_id=prepared.invocation_plan.run_id,
        event_kind=AttemptEventKind.PREFLIGHT_ALLOWED,
        payload={
            "fake_conformance": True,
            "external_provider_invocation_allowed": False,
            "encoded_request_ref": request_ref,
        },
    )
    for attempt_index in range(1, MAXIMUM_PROVIDER_ATTEMPTS + 1):
        store.append_event(
            run_id=prepared.invocation_plan.run_id,
            event_kind=AttemptEventKind.ATTEMPT_STARTED,
            provider_attempt_index=attempt_index,
            payload={
                "encoded_request_sha256": prepared.authorized_request.encoded_request_sha256,
                "simulated": True,
                "external_provider_invoked": False,
            },
        )
        try:
            response = provider.send(prepared.authorized_request)
        except ProviderTransportFailure as exc:
            if exc.code not in RETRYABLE_FAILURES or exc.retryable is not True:
                raise ReplayRunnerError(
                    "FAKE_TRANSPORT_FAILURE_INVALID",
                    "fake transport failures must use the frozen retryable error catalog",
                ) from exc
            chunks = tuple(
                ChunkRecord(
                    chunk_index=index,
                    byte_count=len(chunk),
                    sha256=hashlib.sha256(chunk).hexdigest(),
                    is_final=False,
                    content_ref=store.put_bytes(chunk, media_type="application/octet-stream"),
                )
                for index, chunk in enumerate(exc.chunks)
            )
            exchange = _exchange_for_response(
                prepared,
                provider_attempt_index=attempt_index,
                response_bytes=None,
                metadata={},
                transport_status="FAILED",
                error_code=exc.code,
                retryable=exc.retryable,
                encoded_request_ref=request_ref,
                raw_response_ref=None,
                chunks=chunks,
            )
            exchange_ref = store.put_json(exchange.to_dict())
            for chunk in chunks:
                store.append_event(
                    run_id=prepared.invocation_plan.run_id,
                    event_kind=AttemptEventKind.CHUNK,
                    provider_attempt_index=attempt_index,
                    payload=chunk.to_dict(),
                )
            failed = store.append_event(
                run_id=prepared.invocation_plan.run_id,
                event_kind=AttemptEventKind.FAILED,
                provider_attempt_index=attempt_index,
                payload={
                    "error_code": exc.code,
                    "retryable": exc.retryable,
                    "exchange_ref": exchange_ref,
                },
            )
            if attempt_index < MAXIMUM_PROVIDER_ATTEMPTS:
                continue
            terminal = store.append_event(
                run_id=prepared.invocation_plan.run_id,
                event_kind=AttemptEventKind.TERMINAL,
                provider_attempt_index=attempt_index,
                payload={
                    "terminal_status": TerminalStatus.RETRY_EXHAUSTED.value,
                    "retry_reason": exc.code,
                    "preceding_event_sha256": failed.sha256,
                },
            )
            committed = _commit_terminal(
                store,
                prepared,
                status=TerminalStatus.RETRY_EXHAUSTED,
                attempt_count=attempt_index,
                final_event_sha256=terminal.sha256,
                provider_result=None,
                parser_diagnostics={"parse_outcome": "NOT_RUN"},
                retry_reason=exc.code,
            )
            _validate_completed_run_closure(prepared, store, committed)
            return committed
        response_bytes = bytes(response.response_bytes)
        response_ref = store.put_bytes(response_bytes, media_type="application/octet-stream")
        chunks = _chunk_records(
            response.transport_metadata,
            response_bytes=response_bytes,
            store=store,
        )
        for chunk in chunks:
            store.append_event(
                run_id=prepared.invocation_plan.run_id,
                event_kind=AttemptEventKind.CHUNK,
                provider_attempt_index=attempt_index,
                payload=chunk.to_dict(),
            )
        exchange = _exchange_for_response(
            prepared,
            provider_attempt_index=attempt_index,
            response_bytes=response_bytes,
            metadata=response.transport_metadata,
            transport_status="RETURNED",
            error_code=None,
            retryable=False,
            encoded_request_ref=request_ref,
            raw_response_ref=response_ref,
            chunks=chunks,
        )
        exchange_ref = store.put_json(exchange.to_dict())
        store.append_event(
            run_id=prepared.invocation_plan.run_id,
            event_kind=AttemptEventKind.RETURNED,
            provider_attempt_index=attempt_index,
            payload={"response_ref": response_ref, "exchange_ref": exchange_ref},
        )
        result = provider.normalize(prepared.authorized_request, response)
        result = replace(
            result,
            raw_response_ref={
                **response_ref,
                "schema_version": None,
            },
        )
        validate_provider_result_binding(result, prepared.authorized_request)
        terminal_status = _terminal_status(result)
        parse_kind = (
            AttemptEventKind.PARSED
            if result.status is ProviderResultStatus.RETURNED
            else AttemptEventKind.PARSE_FAILED
        )
        parser_diagnostics = provider.consume_parser_diagnostics()
        parsed = store.append_event(
            run_id=prepared.invocation_plan.run_id,
            event_kind=parse_kind,
            provider_attempt_index=attempt_index,
            payload={
                "provider_result_sha256": canonical_sha256(result.to_dict()),
                "parser_diagnostics": parser_diagnostics,
            },
        )
        terminal = store.append_event(
            run_id=prepared.invocation_plan.run_id,
            event_kind=AttemptEventKind.TERMINAL,
            provider_attempt_index=attempt_index,
            payload={
                "terminal_status": terminal_status.value,
                "retry_reason": None,
                "preceding_event_sha256": parsed.sha256,
                "generated_action_executed": False,
            },
        )
        committed = _commit_terminal(
            store,
            prepared,
            status=terminal_status,
            attempt_count=attempt_index,
            final_event_sha256=terminal.sha256,
            provider_result=result,
            parser_diagnostics=parser_diagnostics,
            retry_reason=None,
        )
        _validate_completed_run_closure(prepared, store, committed)
        return committed
    raise ReplayRunnerError("UNREACHABLE_RETRY_STATE", "provider attempt loop did not terminate")


def execute_live_arm(prepared: PreparedReplayArm) -> None:
    del prepared
    raise ReplayRunnerError(
        "LIVE_EXECUTION_DEFERRED",
        "real provider/GPU validation is intentionally deferred pending owner resource review",
    )
