"""R2.4 Qwen/MAI runtime vertical-slice contracts and registered adapters."""

from mobile_world.runtime.sentinel.r2_4 import contracts as _contracts
from mobile_world.runtime.sentinel.r2_4 import orchestration as _orchestration
from mobile_world.runtime.sentinel.r2_4 import renderer as _renderer
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    R24_MAI_CURRENT_TEXT_UNSUPPORTED_REASON,
    R24_NO_HISTORY_REASON,
    R24_RUNTIME_HISTORY_EXTRACTION_SCHEMA_VERSION,
    R24_RUNTIME_TARGET_DISCOVERY_VERSION,
    RuntimeCodecFactory,
    RuntimeCodecOverlayDeclarationV1,
    RuntimeEditableSpanCodecV1,
    RuntimeHistoryCodecResolverV1,
    RuntimeHistoryExtractionResultV1,
    RuntimeHistoryExtractionStatusV1,
    RuntimeTargetDiscoveryModeV1,
    build_runtime_history_codec_resolver,
    discover_runtime_editable_bindings,
)
from mobile_world.runtime.sentinel.r2_4.contracts import *  # noqa: F403
from mobile_world.runtime.sentinel.r2_4.orchestration import *  # noqa: F403
from mobile_world.runtime.sentinel.r2_4.policy import (
    CpuFakeActiveRuntimePolicyAdapter,
    R22CpuFakeActivePolicyAdapter,
    promote_r22_policy_output,
)
from mobile_world.runtime.sentinel.r2_4.renderer import *  # noqa: F403

__all__ = [
    *_contracts.__all__,
    *_orchestration.__all__,
    *_renderer.__all__,
    "R24_MAI_CURRENT_TEXT_UNSUPPORTED_REASON",
    "R24_NO_HISTORY_REASON",
    "R24_RUNTIME_HISTORY_EXTRACTION_SCHEMA_VERSION",
    "R24_RUNTIME_TARGET_DISCOVERY_VERSION",
    "RuntimeCodecFactory",
    "RuntimeCodecOverlayDeclarationV1",
    "RuntimeEditableSpanCodecV1",
    "RuntimeHistoryExtractionResultV1",
    "RuntimeHistoryExtractionStatusV1",
    "RuntimeHistoryCodecResolverV1",
    "RuntimeTargetDiscoveryModeV1",
    "build_runtime_history_codec_resolver",
    "discover_runtime_editable_bindings",
    "CpuFakeActiveRuntimePolicyAdapter",
    "R22CpuFakeActivePolicyAdapter",
    "promote_r22_policy_output",
]
