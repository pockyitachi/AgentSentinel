"""CPU-only G1.4 state-frozen exact-request replay harness.

The package can run deterministic fake-provider conformance.  Its live
transport path is intentionally fail-only pending separate resource and
run-readiness authorization.
"""

from mobile_world.offline.causal_replay_runner.blinding import (
    BlindingSeal,
    order_blinded_packets,
    prepare_blinding,
    validate_blinded_packet,
)
from mobile_world.offline.causal_replay_runner.capsule_loader import load_replay_capsule
from mobile_world.offline.causal_replay_runner.contracts import (
    ExecutionDomain,
    FakeScenario,
    InvarianceReport,
    InvocationPlan,
    LoadedReplayCapsule,
    ReplayRunnerError,
    ScheduleEntry,
    TerminalStatus,
    UnitKind,
)
from mobile_world.offline.causal_replay_runner.invariance import verify_invariance
from mobile_world.offline.causal_replay_runner.live_preparation import (
    InjectedGpuAssessment,
    LiveModelBinding,
    LivePreparationReceipt,
    OpenAIChatBlockDescriptor,
    OpenAIChatCallDescriptor,
    OpenAIChatResponseProjection,
    VllmLaunchPlan,
    assess_injected_gpu_inventory,
    decode_openai_chat_envelope,
    load_live_preparation,
    prepare_openai_chat_block,
    prepare_openai_chat_call,
    prepare_vllm_launch_plan,
    render_vllm_launch_argv,
)
from mobile_world.offline.causal_replay_runner.provider_codec import (
    DeterministicFakeProviderCodec,
    JsonActionParser,
    OpenAICompatibleProviderCodec,
    ProviderTransportFailure,
    parser_adapter,
)
from mobile_world.offline.causal_replay_runner.runner import (
    PreparedReplayArm,
    build_blinded_packet,
    execute_fake_arm,
    execute_live_arm,
    preflight_block,
    record_preflight_blocked,
)
from mobile_world.offline.causal_replay_runner.schedule import (
    arm_order_for_block,
    logical_run_id,
    schedule_for_unit,
    validate_schedule,
)
from mobile_world.offline.causal_replay_runner.store import ReplayArtifactStore

__all__ = [
    "BlindingSeal",
    "DeterministicFakeProviderCodec",
    "ExecutionDomain",
    "FakeScenario",
    "InvarianceReport",
    "InjectedGpuAssessment",
    "InvocationPlan",
    "JsonActionParser",
    "LiveModelBinding",
    "LivePreparationReceipt",
    "LoadedReplayCapsule",
    "OpenAIChatBlockDescriptor",
    "OpenAIChatCallDescriptor",
    "OpenAICompatibleProviderCodec",
    "OpenAIChatResponseProjection",
    "PreparedReplayArm",
    "ProviderTransportFailure",
    "ReplayArtifactStore",
    "ReplayRunnerError",
    "ScheduleEntry",
    "TerminalStatus",
    "UnitKind",
    "VllmLaunchPlan",
    "arm_order_for_block",
    "assess_injected_gpu_inventory",
    "build_blinded_packet",
    "decode_openai_chat_envelope",
    "execute_fake_arm",
    "execute_live_arm",
    "load_replay_capsule",
    "load_live_preparation",
    "logical_run_id",
    "order_blinded_packets",
    "parser_adapter",
    "preflight_block",
    "prepare_blinding",
    "prepare_openai_chat_block",
    "prepare_openai_chat_call",
    "prepare_vllm_launch_plan",
    "record_preflight_blocked",
    "render_vllm_launch_argv",
    "schedule_for_unit",
    "validate_blinded_packet",
    "validate_schedule",
    "verify_invariance",
]
