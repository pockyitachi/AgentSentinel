"""Provider-free G1.5 checkpoint composed from frozen G1.2/G1.4 interfaces."""

from __future__ import annotations

from dataclasses import dataclass

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    CodecScope,
    ExecutionMode,
    FailurePolicy,
    HistoryIR,
    JsonValue,
    PlanSetProfile,
    PortableContractError,
    ProviderDecision,
    RenderResult,
    TransformationPlan,
    ValidationReceipt,
    canonical_sha256,
)
from mobile_world.offline.causal_replay.core import validate_plan_set, validate_pre_send
from mobile_world.offline.causal_replay.history_codec import HistoryCodec
from mobile_world.offline.causal_replay.registry import HistoryCodecRegistry
from mobile_world.offline.causal_replay_runner.contracts import (
    InvarianceReport,
    LoadedReplayCapsule,
)
from mobile_world.offline.causal_replay_runner.invariance import verify_invariance

CPU_CHECKPOINT_SCHEMA_VERSION = "mobileworld.g1.history-codec-cpu-checkpoint/v1"


@dataclass(frozen=True)
class CpuCheckpointArm:
    arm: ArmKind
    render_result: RenderResult
    validation_receipt: ValidationReceipt
    invariance_report: InvarianceReport

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "arm": self.arm.value,
            "render_result_sha256": canonical_sha256(self.render_result.to_dict()),
            "validation_receipt_sha256": canonical_sha256(self.validation_receipt.to_dict()),
            "invariance_report_sha256": canonical_sha256(self.invariance_report.to_dict()),
            "rendered_request_sha256": self.render_result.rendered_request_sha256,
            "provider_invocation_allowed": self.validation_receipt.provider_invocation_allowed,
            "target_only_diff": self.invariance_report.target_only_diff,
            "source_mapping_reversible": self.invariance_report.source_mapping_reversible,
        }


@dataclass(frozen=True)
class CpuHistoryCodecCheckpoint:
    codec_id: str
    history_ir: HistoryIR
    plan_set_sha256: str
    arms: tuple[CpuCheckpointArm, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": CPU_CHECKPOINT_SCHEMA_VERSION,
            "issue": "ALE-323",
            "story": "G1.5",
            "checkpoint_scope": "CPU_ONLY",
            "codec_id": self.codec_id,
            "history_family": self.history_ir.history_family.value,
            "codec_contract_version": self.history_ir.codec_contract_version,
            "capability_sha256": canonical_sha256(self.history_ir.capabilities.to_dict()),
            "history_ir_sha256": canonical_sha256(self.history_ir.to_dict()),
            "source_request_sha256": self.history_ir.raw_request_sha256,
            "plan_set_sha256": self.plan_set_sha256,
            "arms": [item.to_dict() for item in self.arms],
            "live_smoke_completed": False,
            "provider_invocation_allowed": False,
            "provider_invocation_count": 0,
            "treatment_response_count": 0,
            "gpu_used": False,
            "network_used": False,
            "gui_action_executed": False,
        }


def _validate_frozen_capsule_guards(capsule: LoadedReplayCapsule) -> None:
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
            raise PortableContractError(
                "G15_CAPSULE_GUARD_INVALID",
                f"formal/source capsule safety field {key} must remain exact false",
            )
    count = capsule.source_safety.get("treatment_response_count", 0)
    if type(count) is not int or count != 0:
        raise PortableContractError(
            "G15_CAPSULE_GUARD_INVALID", "treatment response count must remain exact zero"
        )


def run_history_codec_cpu_checkpoint(
    *,
    capsule: LoadedReplayCapsule,
    codec: HistoryCodec,
    paired_plans: tuple[TransformationPlan, ...],
    plan_set_profile: PlanSetProfile = PlanSetProfile.G1_STRICT_MHR,
) -> CpuHistoryCodecCheckpoint:
    """Validate all supplied arms and stop with provider authorization disabled.

    This deliberately has no provider registry, encoder, sender, client, endpoint,
    or response input.  It exercises only the G1.2 render/pre-send proof and the
    G1.4 capsule-aware invariance verifier.
    """

    _validate_frozen_capsule_guards(capsule)
    if codec.capabilities.scope is not CodecScope.LIVE or codec.capabilities.live_ready:
        raise PortableContractError(
            "G15_CPU_CAPABILITY_INVALID",
            "CPU checkpoint requires scope=LIVE and live_ready=false",
        )
    registry = HistoryCodecRegistry()
    registry.register(codec)
    ir = codec.extract(capsule.semantic_request)
    plan_set_sha = validate_plan_set(
        capsule.semantic_request,
        ir,
        paired_plans,
        codec_registry=registry,
        codec_contract_version=codec.contract_version,
        plan_set_profile=plan_set_profile,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    arms: list[CpuCheckpointArm] = []
    for plan in paired_plans:
        rendered = codec.render(
            capsule.semantic_request,
            ir,
            plan,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
        receipt = validate_pre_send(
            capsule.semantic_request,
            ir,
            plan,
            rendered,
            codec_registry=registry,
            codec_contract_version=codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=plan_set_profile,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
        if (
            receipt.provider_invocation_allowed
            or receipt.provider_decision is not ProviderDecision.BLOCK
        ):
            raise PortableContractError(
                "G15_CPU_PROVIDER_AUTHORIZATION_LEAK",
                "CPU checkpoint must remain blocked before provider encoding/send",
            )
        report = verify_invariance(
            capsule=capsule,
            plan=plan,
            render_result=rendered,
            validation_receipt=receipt,
        )
        arms.append(
            CpuCheckpointArm(
                arm=plan.arm,
                render_result=rendered,
                validation_receipt=receipt,
                invariance_report=report,
            )
        )
    return CpuHistoryCodecCheckpoint(
        codec_id=codec.codec_id,
        history_ir=ir,
        plan_set_sha256=plan_set_sha,
        arms=tuple(arms),
    )
