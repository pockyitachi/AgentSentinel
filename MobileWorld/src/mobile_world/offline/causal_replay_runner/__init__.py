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
    "InvocationPlan",
    "JsonActionParser",
    "LoadedReplayCapsule",
    "OpenAICompatibleProviderCodec",
    "PreparedReplayArm",
    "ProviderTransportFailure",
    "ReplayArtifactStore",
    "ReplayRunnerError",
    "ScheduleEntry",
    "TerminalStatus",
    "UnitKind",
    "arm_order_for_block",
    "build_blinded_packet",
    "execute_fake_arm",
    "execute_live_arm",
    "load_replay_capsule",
    "logical_run_id",
    "order_blinded_packets",
    "parser_adapter",
    "preflight_block",
    "prepare_blinding",
    "record_preflight_blocked",
    "schedule_for_unit",
    "validate_blinded_packet",
    "validate_schedule",
    "verify_invariance",
]
