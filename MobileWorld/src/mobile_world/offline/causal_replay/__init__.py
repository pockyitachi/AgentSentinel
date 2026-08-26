"""CPU-only portable Sentinel contract for G1 causal replay.

The package exposes pure transformation and validation interfaces.  It contains
no automatic Sentinel, live history adapter, provider transport, GPU code, or
Collector integration.
"""

from mobile_world.offline.causal_replay.conformance import (
    materialize_fixture_mapping,
    run_fixture_conformance,
)
from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    AuthorizedProviderRequest,
    CapabilityLevel,
    CodecCapabilities,
    CodecScope,
    ExecutionMode,
    FailurePolicy,
    HistoryCodecDeclaration,
    HistoryCodecResolver,
    HistoryFamily,
    HistoryIR,
    OperationKind,
    PlanSetProfile,
    PortableContractError,
    ProviderCodec,
    ProviderCodecResolver,
    ProviderResult,
    ProviderResultStatus,
    ReplaySidecar,
    TransformationPlan,
)
from mobile_world.offline.causal_replay.core import (
    render_request,
    validate_capabilities,
    validate_codec_capabilities,
    validate_history_ir,
    validate_plan,
    validate_plan_set,
    validate_pre_send,
)
from mobile_world.offline.causal_replay.history_codec import (
    DeclarativeFixtureHistoryCodec,
    HistoryCodec,
)
from mobile_world.offline.causal_replay.provider import (
    NoProviderInG12,
    authorize_prepared_request,
    validate_provider_result,
    validate_provider_result_binding,
)
from mobile_world.offline.causal_replay.registry import (
    HistoryCodecRegistry,
    ProviderCodecRegistry,
)
from mobile_world.offline.causal_replay.sidecar import build_sidecar

__all__ = [
    "ArmKind",
    "AuthorizedProviderRequest",
    "CapabilityLevel",
    "CodecCapabilities",
    "CodecScope",
    "DeclarativeFixtureHistoryCodec",
    "ExecutionMode",
    "FailurePolicy",
    "HistoryCodec",
    "HistoryCodecDeclaration",
    "HistoryCodecRegistry",
    "HistoryCodecResolver",
    "HistoryFamily",
    "HistoryIR",
    "OperationKind",
    "PlanSetProfile",
    "NoProviderInG12",
    "PortableContractError",
    "ProviderCodec",
    "ProviderCodecResolver",
    "ProviderCodecRegistry",
    "ProviderResult",
    "ProviderResultStatus",
    "ReplaySidecar",
    "TransformationPlan",
    "build_sidecar",
    "authorize_prepared_request",
    "materialize_fixture_mapping",
    "render_request",
    "run_fixture_conformance",
    "validate_capabilities",
    "validate_codec_capabilities",
    "validate_history_ir",
    "validate_plan",
    "validate_plan_set",
    "validate_pre_send",
    "validate_provider_result",
    "validate_provider_result_binding",
]
