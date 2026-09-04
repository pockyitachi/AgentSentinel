from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.client import (
    AndroidEnvClient,
    CleanupTaskTeardownResultV1,
    CleanupTaskTeardownStatusV1,
)
from mobile_world.runtime.sentinel.r2_4 import (
    production_driver as production_driver_module,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptCostStatusV1,
    LiveAttemptExecutionKindV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    live_attempt_receipt_sha256,
    production_attempt_termination_upper_bound_ns_v1,
)
from mobile_world.runtime.sentinel.r2_4.live_run import LiveSmokeCaseV1, SmokeModeV1
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    MemoryProductionRuntimeAuditSinkV1,
    ProductionRuntimeAuditError,
    ProductionRuntimeAuditV1,
)
from mobile_world.runtime.sentinel.r2_4.run_fatal import (
    ProductionRunFatalError,
    ProductionRunFatalLatchV1,
    build_production_run_fatal_latch_v1,
    production_run_fatal_state_projection,
    production_run_fatal_state_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import PilotHostV1


def _cleanup_invocation(
    *, execution_deadline_ns: int, cleanup_deadline_ns: int
) -> production_driver_module._SmokeInvocationV1:
    return production_driver_module._SmokeInvocationV1(
        manifest_sha256="a" * 64,
        run_id="cleanup-probe-run",
        source_commit="b" * 40,
        host=PilotHostV1.QWEN3_VL,
        sequence_index=0,
        case=LiveSmokeCaseV1(
            case_id="cleanup-probe",
            task_id="cleanup-task",
            mode=SmokeModeV1.ACTIVE,
            request_fixture_path="/tmp/never-read-cleanup-probe.json",
            request_fixture_sha256="c" * 64,
            request_fixture_byte_count=1,
            max_actor_calls=1,
            max_openai_calls=3,
            max_wall_time_seconds=30,
            max_cost_usd_micros=1,
            actor_action_allowed=False,
            provider_final_request_proof_required=True,
        ),
        actor_resource_sha256="d" * 64,
        history_policy_stage_sha256="e" * 64,
        deadline_monotonic_ns=execution_deadline_ns,
        cleanup_deadline_monotonic_ns=cleanup_deadline_ns,
        authority_deadline_monotonic_ns=cleanup_deadline_ns,
        attempt_termination_upper_bound_ns=(production_attempt_termination_upper_bound_ns_v1()),
    )


class _CleanupEnvironment:
    def __init__(self, *, scope_failure: bool = False) -> None:
        self.closed = False
        self.scope_failure = scope_failure
        self.scope_deadlines: list[int] = []
        self.teardown_calls = 0

    @property
    def is_initialized(self) -> bool:
        return True

    @contextmanager
    def request_deadline_scope(self, deadline_monotonic_ns: int) -> Iterator[None]:
        self.scope_deadlines.append(deadline_monotonic_ns)
        if self.scope_failure:
            raise ValueError("deadline scope rejected")
        yield

    def tear_down_task_if_initialized(self, _: str, *, dispatch_started: object = None) -> object:
        assert callable(dispatch_started)
        dispatch_started()
        self.teardown_calls += 1
        return CleanupTaskTeardownResultV1(
            status=CleanupTaskTeardownStatusV1.SUCCEEDED,
            message="closed",
            request_dispatched=True,
        )

    def close(self) -> None:
        self.closed = True


def _cleanup_port(
    invocation: production_driver_module._SmokeInvocationV1,
    *,
    latch: object,
    environment: AndroidEnvClient | _CleanupEnvironment | None,
) -> tuple[object, list[tuple[object, object]]]:
    resource_calls: list[tuple[object, object]] = []
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_run_fatal_latch", latch)
    object.__setattr__(
        port,
        "_resource_lifecycle",
        SimpleNamespace(
            require_dispatch=lambda *args, **kwargs: resource_calls.append((args, kwargs))
        ),
    )
    object.__setattr__(port, "_lock", threading.RLock())
    state = production_driver_module._ProductionUnitStateV1(
        unit_id=port._unit_id(invocation),
        host=invocation.host,
        task_name=invocation.case.task_id,
        deadline_monotonic_ns=invocation.deadline_monotonic_ns,
        cleanup_deadline_monotonic_ns=invocation.cleanup_deadline_monotonic_ns,
        authority_deadline_monotonic_ns=invocation.authority_deadline_monotonic_ns,
        attempt_termination_upper_bound_ns=(invocation.attempt_termination_upper_bound_ns),
        environment=cast(Any, environment),
        observation=None,
    )
    object.__setattr__(port, "_units", {state.unit_id: state})
    object.__setattr__(port, "_unit_journals", {})
    return port, resource_calls


def _unconfirmed_attempt(logical_call_id: str = "fatal-call-1") -> LiveAttemptReceiptV1:
    return LiveAttemptReceiptV1(
        attempt_id="fatal-attempt-1",
        role=LiveAttemptRoleV1.RUBRIC,
        authority_sha256="1" * 64,
        manifest_sha256="2" * 64,
        preflight_sha256="3" * 64,
        case_execution_lease_sha256="4" * 64,
        stage_sha256="5" * 64,
        case_id="fatal-case-1",
        logical_call_id=logical_call_id,
        actor_request_sha256="6" * 64,
        request_sha256="7" * 64,
        transport_binding_sha256="8" * 64,
        pricing_binding_sha256="9" * 64,
        execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
        status=LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
        dispatch_count=1,
        response_envelope_sha256=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
        cost_usd_micros=None,
        cancellation_requested=True,
        termination=LiveAttemptTerminationV1.UNCONFIRMED,
        worker_pid=12345,
        worker_exit_code=None,
        worker_reaped=False,
        late_output_detected=False,
        duration_ns=1,
        failure_code="TERMINATION_UNCONFIRMED",
    )


def _accounting_unknown_attempt(
    status: LiveAttemptStatusV1,
    *,
    logical_call_id: str = "accounting-fatal-call-1",
) -> LiveAttemptReceiptV1:
    source = _unconfirmed_attempt(logical_call_id)
    if status is LiveAttemptStatusV1.FAILED:
        return replace(
            source,
            attempt_id="accounting-failed-attempt-1",
            status=LiveAttemptStatusV1.FAILED,
            cancellation_requested=False,
            termination=LiveAttemptTerminationV1.NONE,
            worker_exit_code=0,
            worker_reaped=True,
            failure_code="PROVIDER_CHILD_PROTOCOL_VIOLATION",
        )
    if status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH:
        return replace(
            source,
            attempt_id="accounting-cancelled-attempt-1",
            status=LiveAttemptStatusV1.CANCELLED_POST_DISPATCH,
            termination=LiveAttemptTerminationV1.TERM,
            worker_exit_code=-15,
            worker_reaped=True,
            failure_code=None,
        )
    raise AssertionError(f"unsupported accounting terminal: {status}")


def test_unconfirmed_attempt_irreversibly_trips_module_owned_latch() -> None:
    attempt = _unconfirmed_attempt()
    latch = build_production_run_fatal_latch_v1()

    state = latch.observe_attempts(
        logical_call_id=attempt.logical_call_id,
        attempts=(attempt,),
    )

    assert state is not None
    assert state.logical_call_id == attempt.logical_call_id
    assert state.attempt_receipt_sha256 == live_attempt_receipt_sha256(attempt)
    projection = cast(dict[str, JsonValue], production_run_fatal_state_projection(state))
    assert projection["failure_code"] == ("TERMINATION_UNCONFIRMED")
    assert len(production_run_fatal_state_sha256(state)) == 64
    assert latch.state == state
    with pytest.raises(ProductionRunFatalError) as raised:
        latch.require_clear()
    assert raised.value.code == "RUN_FATAL_TERMINATION_UNCONFIRMED"


@pytest.mark.parametrize(
    "status",
    (LiveAttemptStatusV1.FAILED, LiveAttemptStatusV1.CANCELLED_POST_DISPATCH),
)
def test_post_dispatch_unknown_accounting_irreversibly_trips_independent_latch(
    status: LiveAttemptStatusV1,
) -> None:
    attempt = _accounting_unknown_attempt(status)
    latch = build_production_run_fatal_latch_v1()

    state = latch.observe_attempts(
        logical_call_id=attempt.logical_call_id,
        attempts=(attempt,),
    )

    assert state is not None
    assert state.failure_code == "LIVE_COST_ACCOUNTING_UNKNOWN"
    assert state.attempt_receipt_sha256 == live_attempt_receipt_sha256(attempt)
    with pytest.raises(ProductionRunFatalError) as raised:
        latch.require_clear()
    assert raised.value.code == "RUN_FATAL_LIVE_COST_ACCOUNTING_UNKNOWN"


def test_zero_dispatch_unknown_accounting_does_not_trip_dispatch_latch() -> None:
    source = _accounting_unknown_attempt(LiveAttemptStatusV1.FAILED)
    attempt = replace(
        source,
        attempt_id="zero-dispatch-accounting-attempt",
        dispatch_count=0,
        cost_status=LiveAttemptCostStatusV1.EXACT,
        cost_usd_micros=0,
        worker_pid=None,
        worker_exit_code=None,
        worker_reaped=False,
        failure_code="PROVIDER_CHILD_START_FAILED",
    )
    latch = build_production_run_fatal_latch_v1()

    assert (
        latch.observe_attempts(
            logical_call_id=attempt.logical_call_id,
            attempts=(attempt,),
        )
        is None
    )
    latch.require_clear()


def test_latch_rejects_caller_construction_and_cross_call_attempts() -> None:
    with pytest.raises(PermissionError):
        ProductionRunFatalLatchV1(_seal=object())

    latch = build_production_run_fatal_latch_v1()
    attempt = _unconfirmed_attempt()
    with pytest.raises(ProductionRunFatalError) as raised:
        latch.observe_attempts(logical_call_id="different-call", attempts=(attempt,))
    assert raised.value.code == "TRACE_BINDING_MISMATCH"
    assert latch.state is None


def test_unconfirmed_worker_blocks_actor_at_final_sdk_gate() -> None:
    latch = build_production_run_fatal_latch_v1()
    attempt = _unconfirmed_attempt()
    latch.observe_attempts(logical_call_id=attempt.logical_call_id, attempts=(attempt,))
    audit = ProductionRuntimeAuditV1(
        policy=None,
        sink=MemoryProductionRuntimeAuditSinkV1(),
        run_fatal_latch=latch,
    )

    with pytest.raises(ProductionRuntimeAuditError) as raised:
        audit.bind_actor_sdk_arguments(
            logical_call_id="next-actor-call",
            result=cast(Any, object()),
            sdk_arguments={},
            collector_request_locator={},
            stream=False,
        )

    assert raised.value.code == "RUN_FATAL_TERMINATION_UNCONFIRMED"


@pytest.mark.parametrize(
    "kind",
    (
        production_driver_module.ProductionDispatchKindV1.ACTOR,
        production_driver_module.ProductionDispatchKindV1.BACKEND_RESET,
        production_driver_module.ProductionDispatchKindV1.BACKEND_TASK_GOAL,
        production_driver_module.ProductionDispatchKindV1.ACTION,
        production_driver_module.ProductionDispatchKindV1.SCORE,
    ),
)
def test_production_port_blocks_every_later_dispatch_after_fatal_latch(
    kind: production_driver_module.ProductionDispatchKindV1,
) -> None:
    latch = build_production_run_fatal_latch_v1()
    attempt = _unconfirmed_attempt()
    latch.observe_attempts(logical_call_id=attempt.logical_call_id, attempts=(attempt,))
    calls: list[object] = []
    resource = SimpleNamespace(
        require_dispatch=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_run_fatal_latch", latch)
    object.__setattr__(port, "_resource_lifecycle", resource)

    with pytest.raises(production_driver_module.ProductionDriverError) as raised:
        port._require_resource_dispatch(
            PilotHostV1.QWEN3_VL,
            kind,
            deadline_ns=time.monotonic_ns() + 1_000_000_000,
        )

    assert raised.value.code == "RUN_FATAL_TERMINATION_UNCONFIRMED"
    assert calls == []


def test_run_fatal_latch_does_not_prevent_cleanup_dispatch() -> None:
    latch = build_production_run_fatal_latch_v1()
    attempt = _unconfirmed_attempt()
    latch.observe_attempts(logical_call_id=attempt.logical_call_id, attempts=(attempt,))
    calls: list[object] = []
    resource = SimpleNamespace(
        require_dispatch=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_run_fatal_latch", latch)
    object.__setattr__(port, "_resource_lifecycle", resource)

    port._require_resource_dispatch(
        PilotHostV1.QWEN3_VL,
        production_driver_module.ProductionDispatchKindV1.CLEANUP,
        deadline_ns=time.monotonic_ns() + 1_000_000_000,
    )

    assert len(calls) == 1


def test_run_fatal_identity_is_bound_into_failed_unit_journal() -> None:
    latch = build_production_run_fatal_latch_v1()
    attempt = _unconfirmed_attempt()
    fatal = latch.observe_attempts(logical_call_id=attempt.logical_call_id, attempts=(attempt,))
    assert fatal is not None
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_run_fatal_latch", latch)
    deadline_ns = time.monotonic_ns() + 1_000_000_000
    state = production_driver_module._ProductionUnitStateV1(
        unit_id="smoke:QWEN3_VL:ACTIVE",
        host=PilotHostV1.QWEN3_VL,
        task_name="fatal-task",
        deadline_monotonic_ns=deadline_ns,
        cleanup_deadline_monotonic_ns=deadline_ns,
        authority_deadline_monotonic_ns=deadline_ns,
        attempt_termination_upper_bound_ns=0,
        environment=None,
        observation=None,
    )

    journal = cast(dict[str, JsonValue], json.loads(port._unit_journal_snapshot(state)))

    assert journal["run_fatal_state"] == production_run_fatal_state_projection(fatal)
    assert journal["run_fatal_state_sha256"] == production_run_fatal_state_sha256(fatal)


def test_fatal_latch_and_expired_execution_still_allow_open_cleanup_teardown() -> None:
    now = time.monotonic_ns()
    invocation = _cleanup_invocation(
        execution_deadline_ns=now - 1,
        cleanup_deadline_ns=now + 8_000_000_000,
    )
    latch = build_production_run_fatal_latch_v1()
    attempt = _unconfirmed_attempt()
    latch.observe_attempts(logical_call_id=attempt.logical_call_id, attempts=(attempt,))
    environment = _CleanupEnvironment()
    port, resource_calls = _cleanup_port(invocation, latch=latch, environment=environment)

    result = port.cleanup_unit(invocation)

    assert len(result.cleanup_receipt_sha256) == 64
    assert environment.scope_deadlines == [invocation.cleanup_deadline_monotonic_ns]
    assert environment.teardown_calls == 1
    assert environment.closed is True
    assert len(resource_calls) == 1
    _, kwargs = resource_calls[0]
    assert kwargs["authority_deadline_monotonic_ns"] == (invocation.cleanup_deadline_monotonic_ns)

    later_invocation = _cleanup_invocation(
        execution_deadline_ns=invocation.deadline_monotonic_ns,
        cleanup_deadline_ns=invocation.cleanup_deadline_monotonic_ns + 1_000_000,
    )
    later_environment = _CleanupEnvironment()
    later_port, _ = _cleanup_port(later_invocation, latch=latch, environment=later_environment)
    later_result = later_port.cleanup_unit(later_invocation)
    assert later_result.cleanup_receipt_sha256 != result.cleanup_receipt_sha256


@pytest.mark.parametrize(
    "status",
    (LiveAttemptStatusV1.FAILED, LiveAttemptStatusV1.CANCELLED_POST_DISPATCH),
)
def test_unknown_cost_fatal_blocks_actor_and_action_but_journals_and_cleans(
    status: LiveAttemptStatusV1,
) -> None:
    now = time.monotonic_ns()
    invocation = _cleanup_invocation(
        execution_deadline_ns=now - 1,
        cleanup_deadline_ns=now + 8_000_000_000,
    )
    latch = build_production_run_fatal_latch_v1()
    attempt = _accounting_unknown_attempt(status)
    fatal = latch.observe_attempts(
        logical_call_id=attempt.logical_call_id,
        attempts=(attempt,),
    )
    assert fatal is not None
    environment = _CleanupEnvironment()
    port, resource_calls = _cleanup_port(invocation, latch=latch, environment=environment)

    for kind in (
        production_driver_module.ProductionDispatchKindV1.ACTOR,
        production_driver_module.ProductionDispatchKindV1.ACTION,
    ):
        with pytest.raises(production_driver_module.ProductionDriverError) as raised:
            port._require_resource_dispatch(
                invocation.host,
                kind,
                deadline_ns=invocation.cleanup_deadline_monotonic_ns,
            )
        assert raised.value.code == "RUN_FATAL_LIVE_COST_ACCOUNTING_UNKNOWN"
    assert resource_calls == []

    result = port.cleanup_unit(invocation)

    assert len(result.cleanup_receipt_sha256) == 64
    assert environment.teardown_calls == 1
    assert environment.closed is True
    assert len(resource_calls) == 1
    unit_id = port._unit_id(invocation)
    journal = cast(dict[str, JsonValue], json.loads(port._unit_journals[unit_id]))
    assert journal["run_fatal_state"] == production_run_fatal_state_projection(fatal)
    assert journal["run_fatal_state_sha256"] == production_run_fatal_state_sha256(fatal)


def test_cleanup_scope_failure_after_resource_reattest_does_not_claim_teardown_attempt(
    tmp_path: Path,
) -> None:
    now = time.monotonic_ns()
    invocation = _cleanup_invocation(
        execution_deadline_ns=now - 1,
        cleanup_deadline_ns=now + 8_000_000_000,
    )
    environment = _CleanupEnvironment(scope_failure=True)
    port, resource_calls = _cleanup_port(
        invocation,
        latch=build_production_run_fatal_latch_v1(),
        environment=environment,
    )
    state = port._units[port._unit_id(invocation)]
    end_task_calls: list[dict[str, object]] = []
    final_path = tmp_path / "scope-failure-collector-final.json"
    final_path.write_text("{}", encoding="utf-8")
    state.task_binding = cast(
        Any,
        SimpleNamespace(
            capture=SimpleNamespace(
                capture_complete=True,
                end_task=lambda **kwargs: end_task_calls.append(kwargs),
            ),
            metadata=SimpleNamespace(task_run_id="scope-failure-task-run"),
        ),
    )
    state.lifecycle = cast(
        Any,
        SimpleNamespace(
            finish_task_attempt=lambda **_: None,
            finalize=lambda **_: final_path,
        ),
    )

    with pytest.raises(production_driver_module.ProductionDriverError) as raised:
        port.cleanup_unit(invocation)

    assert raised.value.code == "TASK_TEARDOWN_FAILED"
    assert len(resource_calls) == 1
    assert environment.teardown_calls == 0
    assert environment.closed is True
    assert len(end_task_calls) == 1
    assert end_task_calls[0]["teardown_attempted"] is False


def test_init_timeout_cleanup_is_no_io_and_archives_typed_recovery_outcome(
    tmp_path: Path,
) -> None:
    urls: list[str] = []

    class _Session:
        closed = False

        def post(self, url: str, **_: object) -> object:
            urls.append(url.removeprefix("http://fixture.invalid"))
            if url.endswith("/init"):
                raise TimeoutError("fixture init timeout")
            raise AssertionError("cleanup must not issue HTTP after unconfirmed init")

        def close(self) -> None:
            self.closed = True

    session = _Session()
    environment = object.__new__(AndroidEnvClient)
    environment._initialized = False
    environment._request_deadline_monotonic_ns = None
    environment._session = cast(Any, session)
    environment.base_url = "http://fixture.invalid"
    environment.device = "emulator-fixture"
    environment._current_task_type = None
    with pytest.raises(TimeoutError, match="fixture init timeout"):
        environment.reset(False)

    now = time.monotonic_ns()
    invocation = _cleanup_invocation(
        execution_deadline_ns=now - 1,
        cleanup_deadline_ns=now + 8_000_000_000,
    )
    port, resource_calls = _cleanup_port(
        invocation,
        latch=build_production_run_fatal_latch_v1(),
        environment=environment,
    )
    state = port._units[port._unit_id(invocation)]
    end_task_calls: list[dict[str, object]] = []
    lifecycle_calls: list[str] = []
    final_path = tmp_path / "init-timeout-collector-final.json"
    final_path.write_text("{}", encoding="utf-8")
    state.task_binding = cast(
        Any,
        SimpleNamespace(
            capture=SimpleNamespace(
                capture_complete=True,
                end_task=lambda **kwargs: end_task_calls.append(kwargs),
            ),
            metadata=SimpleNamespace(task_run_id="init-timeout-task-run"),
        ),
    )
    state.lifecycle = cast(
        Any,
        SimpleNamespace(
            finish_task_attempt=lambda **_: lifecycle_calls.append("finish"),
            finalize=lambda **_: lifecycle_calls.append("finalize") or final_path,
        ),
    )

    with pytest.raises(production_driver_module.ProductionDriverError) as raised:
        port.cleanup_unit(invocation)

    assert raised.value.code == "TASK_TEARDOWN_NOT_INITIALIZED_NO_IO"
    assert urls == ["/init"]
    assert resource_calls == []
    assert session.closed is True
    assert lifecycle_calls == ["finish", "finalize"]
    assert len(end_task_calls) == 1
    assert end_task_calls[0]["teardown_attempted"] is False
    teardown_result = end_task_calls[0]["teardown_result"]
    assert type(teardown_result) is CleanupTaskTeardownResultV1
    assert teardown_result.status is CleanupTaskTeardownStatusV1.NOT_INITIALIZED_NO_IO
    assert teardown_result.request_dispatched is False

    evidence = cast(
        dict[str, JsonValue],
        port.failure_evidence_for_unit(
            invocation,
            failure_phase="CLEANUP",
            failure_code=raised.value.code,
        ),
    )
    outcome = cast(dict[str, JsonValue], evidence["cleanup_recovery_outcome"])
    assert outcome == {
        "initialization_permitted": False,
        "message_sha256": outcome["message_sha256"],
        "outcome": "NOT_INITIALIZED_NO_IO",
        "request_dispatched": False,
        "teardown_attempted": False,
    }
    assert len(cast(str, outcome["message_sha256"])) == 64
    assert len(cast(str, evidence["cleanup_recovery_outcome_sha256"])) == 64


def test_expired_cleanup_blocks_external_teardown_but_closes_and_archives_typed_evidence(
    tmp_path: Path,
) -> None:
    now = time.monotonic_ns()
    invocation = _cleanup_invocation(
        execution_deadline_ns=now - 9_000_000_000,
        cleanup_deadline_ns=now - 1_000_000_000,
    )
    latch = build_production_run_fatal_latch_v1()
    attempt = _unconfirmed_attempt()
    fatal = latch.observe_attempts(logical_call_id=attempt.logical_call_id, attempts=(attempt,))
    assert fatal is not None
    environment = _CleanupEnvironment()
    port, resource_calls = _cleanup_port(invocation, latch=latch, environment=environment)
    state = port._units[port._unit_id(invocation)]
    final_path = tmp_path / "collector-final.json"
    final_path.write_text("{}", encoding="utf-8")
    capture = SimpleNamespace(
        capture_complete=True,
        end_task=lambda **_: None,
    )
    binding = SimpleNamespace(
        capture=capture,
        metadata=SimpleNamespace(task_run_id="cleanup-task-run"),
    )
    lifecycle_calls: list[str] = []
    state.task_binding = cast(Any, binding)
    state.lifecycle = cast(
        Any,
        SimpleNamespace(
            finish_task_attempt=lambda **_: lifecycle_calls.append("finish"),
            finalize=lambda **_: lifecycle_calls.append("finalize") or final_path,
        ),
    )

    with pytest.raises(production_driver_module.ProductionDriverError) as raised:
        port.cleanup_unit(invocation)

    assert raised.value.code == "CLEANUP_DEADLINE_EXCEEDED"
    assert resource_calls == []
    assert environment.scope_deadlines == []
    assert environment.teardown_calls == 0
    assert environment.closed is True
    assert lifecycle_calls == ["finish", "finalize"]
    assert port._unit_id(invocation) not in port._units
    evidence = cast(
        dict[str, JsonValue],
        port.failure_evidence_for_unit(
            invocation,
            failure_phase="CLEANUP",
            failure_code=raised.value.code,
        ),
    )
    assert evidence["failure_code"] == "CLEANUP_DEADLINE_EXCEEDED"
    assert evidence["failure_phase"] == "CLEANUP"
    assert evidence["run_fatal_state"] == production_run_fatal_state_projection(fatal)
    deadline = cast(dict[str, JsonValue], evidence["unit_deadline"])
    assert deadline["execution_deadline_monotonic_ns"] == invocation.deadline_monotonic_ns
    assert deadline["cleanup_deadline_monotonic_ns"] == (invocation.cleanup_deadline_monotonic_ns)
    assert deadline["authority_deadline_monotonic_ns"] == (
        invocation.authority_deadline_monotonic_ns
    )
    assert deadline["cleanup_within_owner_authority"] is True
    assert deadline["attempt_termination_upper_bound_ns"] == (
        production_attempt_termination_upper_bound_ns_v1()
    )
    assert deadline["teardown_budget_ns"] == 1_000_000_000
    assert deadline["teardown_budget_positive"] is True
    assert len(cast(str, deadline["deadline_binding_sha256"])) == 64


def test_expired_smoke_without_environment_closes_local_runtime_and_cannot_succeed(
    tmp_path: Path,
) -> None:
    now = time.monotonic_ns()
    invocation = _cleanup_invocation(
        execution_deadline_ns=now - 9_000_000_000,
        cleanup_deadline_ns=now - 1_000_000_000,
    )
    port, resource_calls = _cleanup_port(
        invocation,
        latch=build_production_run_fatal_latch_v1(),
        environment=None,
    )
    state = port._units[port._unit_id(invocation)]
    agent_calls: list[str] = []
    state.agent = cast(
        Any,
        SimpleNamespace(
            done=lambda: agent_calls.append("done"),
            openai_client=SimpleNamespace(close=lambda: agent_calls.append("client-close")),
            get_total_token_usage=lambda: {},
        ),
    )
    final_path = tmp_path / "smoke-collector-final.json"
    final_path.write_text("{}", encoding="utf-8")
    lifecycle_calls: list[str] = []
    state.task_binding = cast(
        Any,
        SimpleNamespace(
            capture=SimpleNamespace(capture_complete=True, end_task=lambda **_: None),
            metadata=SimpleNamespace(task_run_id="smoke-cleanup-task-run"),
        ),
    )
    state.lifecycle = cast(
        Any,
        SimpleNamespace(
            finish_task_attempt=lambda **_: lifecycle_calls.append("finish"),
            finalize=lambda **_: lifecycle_calls.append("finalize") or final_path,
        ),
    )

    with pytest.raises(production_driver_module.ProductionDriverError) as raised:
        port.cleanup_unit(invocation)

    assert raised.value.code == "CLEANUP_DEADLINE_EXCEEDED"
    assert resource_calls == []
    assert agent_calls == ["done", "client-close"]
    assert lifecycle_calls == ["finish", "finalize"]
    assert port._unit_id(invocation) not in port._units
    evidence = cast(
        dict[str, JsonValue],
        port.failure_evidence_for_unit(
            invocation,
            failure_phase="CLEANUP",
            failure_code=raised.value.code,
        ),
    )
    assert evidence["failure_code"] == "CLEANUP_DEADLINE_EXCEEDED"
    assert evidence["unit_deadline"] is not None
