"""Runtime Sentinel package.

Nothing is imported eagerly here.  Ordinary flat evaluation must not load the
retired multi-generation runtime merely because Python initializes this
package.  Historical public names remain available lazily to offline tests.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_CONTRACT_NAMES = {
    "SENTINEL_RECEIPT_SCHEMA_VERSION",
    "SENTINEL_RUNTIME_CONTRACT_VERSION",
    "SentinelBypassReason",
    "SentinelCallRole",
    "SentinelContext",
    "SentinelContractError",
    "SentinelDecision",
    "SentinelDecisionKind",
    "SentinelFallbackReason",
    "SentinelHostConfig",
    "SentinelMode",
    "SentinelPolicy",
    "SentinelPolicyOutput",
    "SentinelReceipt",
    "SentinelReceiptSink",
    "SentinelReceiptTransaction",
    "SentinelResult",
    "SentinelValidationStatus",
}
_STATE_NAMES = {
    "ExecutionStateChannelReceiptV1",
    "ExecutionStateChannelStatusV1",
    "ExecutionStateCompositeResultV1",
    "ExecutionStateFallbackReasonV1",
    "ExternalExecutionStateReceiptSinkV1",
    "MemoryExecutionStateReceiptSinkV1",
}
_POLICY_NAMES = {"DeterministicFakeSentinelPolicy", "NoOpSentinelPolicy"}
_SEAM_NAMES = {
    "PromptSentinel",
    "SentinelGlobalSwitch",
    "SentinelLogicalCall",
    "bind_sentinel_logical_call",
    "current_sentinel_logical_call",
}
_FLAT_CONTROL_NAMES = {
    "GLOBAL_SENTINEL_KILL_SWITCH",
    "global_sentinel_kill_switch_active",
    "set_global_sentinel_kill_switch",
}
_SIDECAR_NAMES = {"ExternalSentinelReceiptSink", "MemorySentinelReceiptSink"}


def __getattr__(name: str) -> Any:
    if name in _CONTRACT_NAMES:
        return getattr(import_module("mobile_world.runtime.sentinel.contracts"), name)
    if name in _STATE_NAMES:
        return getattr(import_module("mobile_world.runtime.sentinel.execution_state_channel"), name)
    if name in _POLICY_NAMES:
        return getattr(import_module("mobile_world.runtime.sentinel.policies"), name)
    if name in _SEAM_NAMES:
        return getattr(import_module("mobile_world.runtime.sentinel.seam"), name)
    if name in _FLAT_CONTROL_NAMES:
        return getattr(import_module("mobile_world.runtime.sentinel.lean_runtime"), name)
    if name in _SIDECAR_NAMES:
        return getattr(import_module("mobile_world.runtime.sentinel.sidecar"), name)
    raise AttributeError(name)


__all__ = [
    *sorted(_CONTRACT_NAMES),
    *sorted(_FLAT_CONTROL_NAMES),
    *sorted(_STATE_NAMES),
    *sorted(_POLICY_NAMES),
    *sorted(_SEAM_NAMES),
    *sorted(_SIDECAR_NAMES),
]
