"""Raw audit event envelope primitives.

This module implements only the common envelope from
``mobileworld.audit.event/v1``.  Event-specific payload schemas remain the
responsibility of the corresponding capture surface.  In particular, opaque
application data inside ``payload`` is deliberately not recursively linted:
model messages, tool schemas, and provider responses may legitimately contain
words or keys reserved for the future evaluation layer.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = "mobileworld.audit.event/v1"

EVENT_TYPES = frozenset(
    {
        "run_started",
        "task_started",
        "step_started",
        "adapter_state_snapshot",
        "model_request",
        "model_stream_chunk",
        "model_response",
        "model_attempt_failed",
        "agent_decision",
        "action_execution_started",
        "transition_completed",
        "transition_failed",
        "transition_not_executed",
        "collector_error",
        "task_ended",
        "run_ended",
    }
)

# Keep this tuple in the contract's documented order.  JSON object ordering is
# not semantic, but deterministic construction makes raw artifacts easier to
# inspect and test.
COMMON_EVENT_FIELDS = (
    "schema_version",
    "event_id",
    "event_type",
    "run_id",
    "task_run_id",
    "stream_id",
    "seq",
    "wall_time",
    "monotonic_ns",
    "caused_by_event_id",
    "producer",
    "payload",
)
COMMON_EVENT_FIELD_SET = frozenset(COMMON_EVENT_FIELDS)

PRODUCER_FIELDS = ("component", "version", "process_id", "worker_id")
PRODUCER_FIELD_SET = frozenset(PRODUCER_FIELDS)

RESERVED_EVALUATION_LABEL_KEYS = frozenset(
    {
        "history_status",
        "uptake_evidence",
        "downstream_effect",
        "severity",
        "rubric_alignment",
        "sentinel_verdict",
        "keep",
        "drop",
        "replace",
        "abstain",
    }
)

_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


class SchemaValidationError(ValueError):
    """Raised when collector-defined raw schema metadata is invalid."""


@dataclass(frozen=True, slots=True)
class Producer:
    """The exact producer object embedded in every raw event envelope."""

    component: str
    version: str
    process_id: int
    worker_id: str

    @classmethod
    def local(cls, *, version: str, worker_id: str) -> Producer:
        """Create producer metadata for the current process without I/O."""

        return cls(
            component="mobile_world.audit",
            version=version,
            process_id=os.getpid(),
            worker_id=worker_id,
        )

    def to_dict(self) -> dict[str, str | int]:
        """Return the producer using the contract's exact key set."""

        value: dict[str, str | int] = {
            "component": self.component,
            "version": self.version,
            "process_id": self.process_id,
            "worker_id": self.worker_id,
        }
        _validate_producer(value)
        return value


def utc_wall_time() -> str:
    """Return an RFC3339 UTC timestamp suitable for an event envelope."""

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_event(
    *,
    event_id: str,
    event_type: str,
    run_id: str,
    task_run_id: str | None,
    seq: int,
    producer: Producer | Mapping[str, Any],
    payload: Mapping[str, Any],
    stream_id: str | None = None,
    wall_time: str | datetime | None = None,
    monotonic_ns: int | None = None,
    caused_by_event_id: str | None = None,
) -> dict[str, Any]:
    """Build and validate one exact v1 common event envelope.

    IDs and sequence numbers are supplied by the recorder, which owns stream
    ordering and uniqueness.  ``stream_id`` is derived from ``task_run_id``
    when omitted.  The payload receives only a shallow mapping copy; this
    function neither serializes nor recursively inspects captured application
    data.
    """

    if isinstance(producer, Producer):
        producer_value = producer.to_dict()
    else:
        producer_value = dict(producer)

    if isinstance(wall_time, datetime):
        if wall_time.tzinfo is None or wall_time.utcoffset() is None:
            raise SchemaValidationError("wall_time datetime must include a timezone")
        wall_time_value = wall_time.isoformat(timespec="microseconds")
    else:
        wall_time_value = wall_time if wall_time is not None else utc_wall_time()

    effective_stream_id = stream_id or (task_run_id if task_run_id is not None else run_id)
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "run_id": run_id,
        "task_run_id": task_run_id,
        "stream_id": effective_stream_id,
        "seq": seq,
        "wall_time": wall_time_value,
        "monotonic_ns": time.monotonic_ns() if monotonic_ns is None else monotonic_ns,
        "caused_by_event_id": caused_by_event_id,
        "producer": producer_value,
        "payload": dict(payload),
    }
    validate_event_envelope(event)
    return event


def validate_event_envelope(event: Mapping[str, Any]) -> None:
    """Validate only the exact common v1 envelope and producer metadata.

    The event-specific ``payload`` is checked to be a mapping but is not
    recursively scanned.  This is intentional: it may contain faithfully
    captured opaque data whose keys overlap future evaluation terminology.
    Call :func:`validate_collector_metadata_keys` separately and only on a
    known collector-defined metadata structure.
    """

    actual_fields = frozenset(event)
    if actual_fields != COMMON_EVENT_FIELD_SET:
        missing = sorted(COMMON_EVENT_FIELD_SET - actual_fields)
        extra = sorted(actual_fields - COMMON_EVENT_FIELD_SET)
        raise SchemaValidationError(
            f"event envelope fields must match v1 exactly; missing={missing}, extra={extra}"
        )

    if event["schema_version"] != SCHEMA_VERSION:
        raise SchemaValidationError(
            f"schema_version must be {SCHEMA_VERSION!r}, got {event['schema_version']!r}"
        )

    _require_identifier(event["event_id"], "event_id")
    _require_identifier(event["run_id"], "run_id")

    task_run_id = event["task_run_id"]
    if task_run_id is not None:
        _require_identifier(task_run_id, "task_run_id")

    stream_id = event["stream_id"]
    _require_identifier(stream_id, "stream_id")
    expected_stream_id = task_run_id if task_run_id is not None else event["run_id"]
    if stream_id != expected_stream_id:
        raise SchemaValidationError(
            "stream_id must equal task_run_id for task events and run_id for run-only events"
        )

    event_type = event["event_type"]
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise SchemaValidationError(f"unsupported v1 event_type: {event_type!r}")

    seq = event["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise SchemaValidationError("seq must be a positive integer")

    wall_time = event["wall_time"]
    if not isinstance(wall_time, str) or not _is_rfc3339_with_timezone(wall_time):
        raise SchemaValidationError("wall_time must be an RFC3339 timestamp with timezone")

    monotonic_ns = event["monotonic_ns"]
    if isinstance(monotonic_ns, bool) or not isinstance(monotonic_ns, int) or monotonic_ns < 0:
        raise SchemaValidationError("monotonic_ns must be a non-negative integer")

    caused_by_event_id = event["caused_by_event_id"]
    if caused_by_event_id is not None:
        _require_identifier(caused_by_event_id, "caused_by_event_id")

    producer = event["producer"]
    if not isinstance(producer, Mapping):
        raise SchemaValidationError("producer must be a mapping")
    _validate_producer(producer)

    if not isinstance(event["payload"], Mapping):
        raise SchemaValidationError("payload must be a mapping")


def validate_collector_metadata_keys(
    metadata: Mapping[str, Any],
    *,
    opaque_keys: Collection[str] = (),
) -> None:
    """Reject evaluation-label keys in known collector metadata only.

    Callers must pass a collector-defined metadata object, not an entire event
    or captured request/response graph.  Values below keys listed in
    ``opaque_keys`` are preserved without inspection.  Matching is
    case-insensitive and applies to mapping keys, never to ordinary string
    values.
    """

    opaque = {key.casefold() for key in opaque_keys}
    _validate_metadata_node(metadata, path="$", opaque_keys=opaque, seen=set())


# A short alias keeps call sites readable while retaining the more explicit
# public name above.
lint_collector_metadata = validate_collector_metadata_keys


def _validate_metadata_node(
    value: Any,
    *,
    path: str,
    opaque_keys: set[str],
    seen: set[int],
) -> None:
    if isinstance(value, Mapping):
        object_id = id(value)
        if object_id in seen:
            return
        seen.add(object_id)
        for key, child in value.items():
            if not isinstance(key, str):
                raise SchemaValidationError(f"collector metadata key at {path} must be a string")
            folded_key = key.casefold()
            child_path = f"{path}.{key}"
            if folded_key in RESERVED_EVALUATION_LABEL_KEYS:
                raise SchemaValidationError(
                    f"reserved evaluation-label key {folded_key!r} "
                    f"(source key {key!r}) is forbidden at {child_path}"
                )
            if folded_key not in opaque_keys:
                _validate_metadata_node(
                    child,
                    path=child_path,
                    opaque_keys=opaque_keys,
                    seen=seen,
                )
        return

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        object_id = id(value)
        if object_id in seen:
            return
        seen.add(object_id)
        for index, child in enumerate(value):
            _validate_metadata_node(
                child,
                path=f"{path}[{index}]",
                opaque_keys=opaque_keys,
                seen=seen,
            )


def _validate_producer(producer: Mapping[str, Any]) -> None:
    actual_fields = frozenset(producer)
    if actual_fields != PRODUCER_FIELD_SET:
        missing = sorted(PRODUCER_FIELD_SET - actual_fields)
        extra = sorted(actual_fields - PRODUCER_FIELD_SET)
        raise SchemaValidationError(
            f"producer fields must match v1 exactly; missing={missing}, extra={extra}"
        )

    component = producer["component"]
    version = producer["version"]
    process_id = producer["process_id"]
    worker_id = producer["worker_id"]
    if component != "mobile_world.audit":
        raise SchemaValidationError("producer.component must be 'mobile_world.audit'")
    if not isinstance(version, str) or not version:
        raise SchemaValidationError("producer.version must be a non-empty string")
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id < 1:
        raise SchemaValidationError("producer.process_id must be a positive integer")
    if not isinstance(worker_id, str) or not worker_id:
        raise SchemaValidationError("producer.worker_id must be a non-empty string")
    if any(marker in worker_id for marker in ("://", "?", "\n", "\r")):
        raise SchemaValidationError(
            "producer.worker_id must be non-secret and must not contain URLs or query strings"
        )


def _require_identifier(value: Any, field: str) -> None:
    if not isinstance(value, str) or not (_is_uuid7(value) or _ULID_RE.fullmatch(value)):
        raise SchemaValidationError(f"{field} must be a UUIDv7 or canonical ULID")


def _is_uuid7(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.version == 7 and parsed.variant == uuid.RFC_4122


def _is_rfc3339_with_timezone(value: str) -> bool:
    if _RFC3339_RE.fullmatch(value) is None:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None
