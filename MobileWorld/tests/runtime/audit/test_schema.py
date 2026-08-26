from copy import deepcopy
from datetime import datetime

import pytest

from mobile_world.runtime.audit.schemas import (
    COMMON_EVENT_FIELD_SET,
    SCHEMA_VERSION,
    Producer,
    SchemaValidationError,
    build_event,
    validate_collector_metadata_keys,
    validate_event_envelope,
)

RUN_ID = "0198a000-0000-7000-8000-000000000001"
TASK_RUN_ID = "0198a000-0000-7000-8000-000000000002"
EVENT_ID = "0198a000-0000-7000-8000-000000000101"
CAUSE_ID = "0198a000-0000-7000-8000-000000000100"
WALL_TIME = "2026-08-18T14:00:01.000000-04:00"


def _producer() -> Producer:
    return Producer(
        component="mobile_world.audit",
        version="0.1.0",
        process_id=42,
        worker_id="worker-01",
    )


def _event(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "event_id": EVENT_ID,
        "event_type": "step_started",
        "run_id": RUN_ID,
        "task_run_id": TASK_RUN_ID,
        "seq": 1,
        "producer": _producer(),
        "payload": {"step_id": "opaque-step", "observation": {}},
        "wall_time": WALL_TIME,
        "monotonic_ns": 123,
        "caused_by_event_id": CAUSE_ID,
    }
    arguments.update(overrides)
    return build_event(**arguments)  # type: ignore[arg-type]


def test_build_event_has_the_exact_v1_common_envelope() -> None:
    event = _event()

    assert frozenset(event) == COMMON_EVENT_FIELD_SET
    assert event["schema_version"] == SCHEMA_VERSION
    assert event["stream_id"] == TASK_RUN_ID
    assert event["producer"] == {
        "component": "mobile_world.audit",
        "version": "0.1.0",
        "process_id": 42,
        "worker_id": "worker-01",
    }
    validate_event_envelope(event)


def test_run_event_derives_run_stream_id() -> None:
    event = _event(event_type="run_started", task_run_id=None, caused_by_event_id=None)

    assert event["stream_id"] == RUN_ID
    validate_event_envelope(event)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "mobileworld.audit.event/v2"),
        ("event_id", "event-1"),
        ("stream_id", RUN_ID),
        ("seq", 0),
        ("wall_time", "2026-08-18T14:00:01"),
        ("monotonic_ns", -1),
    ],
)
def test_invalid_common_envelope_value_is_rejected(field: str, value: object) -> None:
    event = _event()
    event[field] = value

    with pytest.raises(SchemaValidationError):
        validate_event_envelope(event)


def test_extra_or_missing_common_envelope_fields_are_rejected() -> None:
    event = _event()
    with_extra = {**event, "step_id": "must-live-in-payload"}
    without_payload = deepcopy(event)
    del without_payload["payload"]

    with pytest.raises(SchemaValidationError, match="extra=.*step_id"):
        validate_event_envelope(with_extra)
    with pytest.raises(SchemaValidationError, match="missing=.*payload"):
        validate_event_envelope(without_payload)


def test_producer_component_is_contract_fixed() -> None:
    event = _event()
    event["producer"] = {**event["producer"], "component": "another.collector"}

    with pytest.raises(SchemaValidationError, match="mobile_world.audit"):
        validate_event_envelope(event)


def test_naive_datetime_is_rejected_by_builder() -> None:
    with pytest.raises(SchemaValidationError, match="timezone"):
        _event(wall_time=datetime(2026, 8, 18, 14, 0, 1))


def test_label_lint_is_scoped_to_collector_metadata_not_opaque_capture() -> None:
    # Reserved terminology in captured model/tool data must remain lossless.
    event = _event(
        payload={
            "request_view": {
                "messages": [{"role": "user", "content": "keep this text"}],
                "tools": [{"keep": {"type": "boolean"}}],
            }
        }
    )
    validate_event_envelope(event)

    validate_collector_metadata_keys(
        {
            "capture_complete": True,
            "opaque_request": {"KEEP": "application-defined key"},
        },
        opaque_keys={"opaque_request"},
    )

    with pytest.raises(SchemaValidationError, match="severity"):
        validate_collector_metadata_keys(
            {"capture_complete": False, "diagnostics": [{"SeVeRiTy": "high"}]}
        )


def test_label_lint_checks_keys_not_ordinary_string_values() -> None:
    validate_collector_metadata_keys(
        {"capture_note": "keep/drop/replace are words in this factual diagnostic"}
    )
