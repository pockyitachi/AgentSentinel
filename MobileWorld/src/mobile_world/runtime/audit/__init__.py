"""Passive, lossless runtime audit collection for MobileWorld.

The package intentionally contains no history classification, rubric, or
prompt-intervention logic.  Runtime hooks are opt-in and persist only observed
application-layer model I/O and environment transitions.
"""

from mobile_world.runtime.audit.config import AuditConfig, CollectorMode
from mobile_world.runtime.audit.context import AuditContext, ModelCallTrace, bind_audit_context
from mobile_world.runtime.audit.null_recorder import NullRecorder

__all__ = [
    "AuditConfig",
    "AuditContext",
    "CollectorMode",
    "ModelCallTrace",
    "NullRecorder",
    "bind_audit_context",
]
