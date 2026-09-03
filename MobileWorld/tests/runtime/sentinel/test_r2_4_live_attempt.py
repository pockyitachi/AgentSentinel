from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import replace
from typing import Any, cast

import pytest

from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    CpuFixedAttemptScriptV1,
    CpuFixedCancellableAttemptRunnerV1,
    CpuFixedLiveAttemptHandleV1,
    CpuFixedLiveAttemptRunnerV1,
    LiveAttemptAuthorityV1,
    LiveAttemptCostStatusV1,
    LiveAttemptError,
    LiveAttemptPricingV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    MemoryLiveAttemptReceiptSinkV1,
    ProductionHistoryPolicyAttemptRunnerV1,
    build_canonical_history_policy_request,
    live_attempt_authority_projection,
    live_attempt_authority_sha256,
    live_attempt_cost_usd_micros,
    live_attempt_pricing_sha256,
    live_attempt_receipt_projection,
    live_attempt_receipt_root_sha256,
    live_attempt_receipt_sha256,
    production_live_attempt_runner_available_v1,
    snapshot_live_attempt_authority,
    snapshot_live_attempt_receipt,
)
from mobile_world.runtime.sentinel.seam import _PolicyExecutionFence


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _authority(
    suffix: str,
    *,
    deadline_seconds: float = 5.0,
    role: LiveAttemptRoleV1 = LiveAttemptRoleV1.HISTORY_POLICY,
) -> LiveAttemptAuthorityV1:
    return LiveAttemptAuthorityV1(
        attempt_id=f"attempt-{suffix}",
        role=role,
        manifest_sha256=_digest("manifest"),
        preflight_sha256=_digest("preflight"),
        case_execution_lease_sha256=_digest(f"lease-{suffix}"),
        stage_sha256=_digest(f"stage-{role.value}"),
        case_id=f"case-{suffix}",
        logical_call_id=f"logical-{suffix}",
        actor_request_sha256=_digest(f"actor-request-{suffix}"),
        request_sha256=_digest(f"request-{suffix}"),
        transport_binding_sha256=_digest(f"transport-{suffix}"),
        pricing_binding_sha256=_digest("pinned-price-table"),
        deadline_monotonic_ns=time.monotonic_ns() + round(deadline_seconds * 1_000_000_000),
        max_cost_usd_micros=100,
        max_output_tokens=32,
    )


def _runner() -> tuple[CpuFixedLiveAttemptRunnerV1, MemoryLiveAttemptReceiptSinkV1]:
    sink = MemoryLiveAttemptReceiptSinkV1()
    return (
        CpuFixedLiveAttemptRunnerV1(
            sink=sink,
            startup_timeout_ms=1_000,
            cancel_grace_ms=100,
        ),
        sink,
    )


def _begin(
    runner: CpuFixedLiveAttemptRunnerV1,
    authority: LiveAttemptAuthorityV1,
    script: CpuFixedAttemptScriptV1,
) -> CpuFixedLiveAttemptHandleV1:
    return runner.begin(
        authority,
        confirmed_authority_sha256=live_attempt_authority_sha256(authority),
        script=script,
    )


def _cancel_after_dispatch(
    handle: CpuFixedLiveAttemptHandleV1,
) -> tuple[LiveAttemptReceiptV1, LiveAttemptReceiptV1]:
    outcomes: list[LiveAttemptReceiptV1] = []

    def execute() -> None:
        outcomes.append(handle.execute())

    worker = threading.Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 2.0
    while handle.dispatch_count == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert handle.dispatch_count == 1
    cancelled = handle.cancel_and_join()
    worker.join(2.0)
    assert not worker.is_alive()
    assert outcomes == [cancelled]
    return cancelled, outcomes[0]


def test_authority_and_receipt_projections_are_hash_bound_and_detached() -> None:
    authority = _authority("projection")
    detached = snapshot_live_attempt_authority(authority)
    assert detached == authority
    assert detached is not authority
    assert live_attempt_authority_sha256(authority) == live_attempt_authority_sha256(detached)
    assert live_attempt_authority_projection(authority)["request_sha256"] == (
        authority.request_sha256
    )
    assert live_attempt_authority_projection(authority)["actor_request_sha256"] == (
        authority.actor_request_sha256
    )
    assert live_attempt_authority_projection(authority)["case_execution_lease_sha256"] == (
        authority.case_execution_lease_sha256
    )

    runner, _ = _runner()
    receipt = _begin(runner, authority, CpuFixedAttemptScriptV1.COMPLETE_ONCE).execute()
    receipt_copy = snapshot_live_attempt_receipt(receipt)
    assert receipt_copy == receipt
    assert receipt_copy is not receipt
    assert live_attempt_receipt_sha256(receipt) == live_attempt_receipt_sha256(receipt_copy)
    assert live_attempt_receipt_projection(receipt)["authority_sha256"] == (
        live_attempt_authority_sha256(authority)
    )


def test_cancel_before_dispatch_is_exact_zero_and_reaped() -> None:
    authority = _authority("pre-cancel")
    runner, sink = _runner()
    handle = _begin(runner, authority, CpuFixedAttemptScriptV1.BLOCK_AFTER_DISPATCH)

    receipt = handle.cancel_and_join()

    assert receipt.status is LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH
    assert receipt.dispatch_count == 0
    assert receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert receipt.cost_usd_micros == 0
    assert receipt.termination is LiveAttemptTerminationV1.COOPERATIVE
    assert receipt.worker_reaped
    assert receipt.worker_exit_code == 0
    assert not receipt.late_output_detected
    assert not receipt.passed
    assert receipt.accounting_complete
    assert not handle.worker_alive
    assert sink.started_count == sink.terminal_count == 1
    assert sink.receipts == (receipt,)
    assert sink.receipt_root_sha256 == live_attempt_receipt_root_sha256((receipt,))


def test_fixed_cpu_attempt_completes_exactly_one_dispatch() -> None:
    authority = _authority("complete")
    runner, sink = _runner()

    receipt = _begin(runner, authority, CpuFixedAttemptScriptV1.COMPLETE_ONCE).execute()

    assert receipt.status is LiveAttemptStatusV1.COMPLETED
    assert receipt.dispatch_count == 1
    assert receipt.response_envelope_sha256 is not None
    assert (receipt.input_tokens, receipt.output_tokens, receipt.total_tokens) == (7, 3, 10)
    assert receipt.cached_input_tokens == 0
    assert receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert receipt.cost_usd_micros == 1
    assert receipt.termination is LiveAttemptTerminationV1.NONE
    assert receipt.worker_reaped and receipt.worker_exit_code == 0
    assert receipt.passed and receipt.accounting_complete
    assert sink.receipt_for(authority.attempt_id) == receipt


def test_known_provider_overrun_remains_exactly_accounted_while_failed() -> None:
    authority = _authority("known-overrun")
    runner, _ = _runner()
    completed = _begin(runner, authority, CpuFixedAttemptScriptV1.COMPLETE_ONCE).execute()

    failed = replace(
        completed,
        status=LiveAttemptStatusV1.FAILED,
        failure_code="PROVIDER_RESULT_EXCEEDS_AUTHORITY",
    )

    assert failed.cost_status is LiveAttemptCostStatusV1.EXACT
    assert failed.cost_usd_micros == completed.cost_usd_micros
    assert failed.input_tokens == completed.input_tokens
    assert failed.output_tokens == completed.output_tokens
    assert failed.accounting_complete
    assert not failed.passed
    with pytest.raises(LiveAttemptError) as hidden:
        replace(
            failed,
            cost_status=LiveAttemptCostStatusV1.UNKNOWN,
            cost_usd_micros=None,
        )
    assert hidden.value.code == "INVALID_NONCOMPLETED_RECEIPT"


def test_post_dispatch_cancel_uses_term_waitpid_and_never_publishes_late() -> None:
    authority = _authority("term")
    runner, sink = _runner()
    handle = _begin(runner, authority, CpuFixedAttemptScriptV1.BLOCK_AFTER_DISPATCH)

    receipt, _ = _cancel_after_dispatch(handle)

    assert receipt.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
    assert receipt.dispatch_count == 1
    assert receipt.termination is LiveAttemptTerminationV1.TERM
    assert receipt.worker_reaped
    assert receipt.worker_exit_code is not None and receipt.worker_exit_code < 0
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert receipt.cost_usd_micros is None
    assert not receipt.accounting_complete
    assert not receipt.passed
    assert not receipt.late_output_detected
    assert not handle.worker_alive
    terminal_count = sink.terminal_count
    receipt_root = sink.receipt_root_sha256
    time.sleep(0.05)
    assert sink.terminal_count == terminal_count == 1
    assert sink.receipt_root_sha256 == receipt_root
    assert handle.terminal_receipt == receipt


def test_post_dispatch_cancel_escalates_to_kill_and_waitpid() -> None:
    authority = _authority("kill")
    runner, sink = _runner()
    handle = _begin(runner, authority, CpuFixedAttemptScriptV1.IGNORE_TERM_AFTER_DISPATCH)

    receipt, _ = _cancel_after_dispatch(handle)

    assert receipt.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
    assert receipt.termination is LiveAttemptTerminationV1.KILL
    assert receipt.worker_reaped
    assert receipt.worker_exit_code is not None and receipt.worker_exit_code < 0
    assert receipt.dispatch_count == 1
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert not receipt.accounting_complete and not receipt.passed
    assert not receipt.late_output_detected
    assert not handle.worker_alive
    assert sink.receipts == (receipt,)


def test_unknown_post_dispatch_cost_cannot_be_relabelled_exact_or_passed() -> None:
    authority = _authority("unknown")
    runner, _ = _runner()
    handle = _begin(runner, authority, CpuFixedAttemptScriptV1.BLOCK_AFTER_DISPATCH)
    receipt, _ = _cancel_after_dispatch(handle)
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert not receipt.passed

    with pytest.raises(LiveAttemptError) as raised:
        replace(
            receipt,
            cost_status=LiveAttemptCostStatusV1.EXACT,
            cost_usd_micros=0,
        )
    assert raised.value.code == "INVALID_CANCELLED_RECEIPT"


def test_authority_hash_drift_and_open_script_injection_fail_before_start() -> None:
    authority = _authority("drift")
    runner, sink = _runner()

    with pytest.raises(LiveAttemptError) as drift:
        runner.begin(
            authority,
            confirmed_authority_sha256=_digest("another-authority"),
            script=CpuFixedAttemptScriptV1.COMPLETE_ONCE,
        )
    assert drift.value.code == "AUTHORITY_HASH_DRIFT"
    assert sink.started_count == 0

    with pytest.raises(LiveAttemptError) as script:
        runner.begin(
            authority,
            confirmed_authority_sha256=live_attempt_authority_sha256(authority),
            script=cast(Any, "python -c arbitrary"),
        )
    assert script.value.code == "UNTRUSTED_CPU_SCRIPT"
    assert sink.started_count == 0

    with pytest.raises(TypeError):
        cast(Any, runner.begin)(
            authority,
            confirmed_authority_sha256=live_attempt_authority_sha256(authority),
            script=CpuFixedAttemptScriptV1.COMPLETE_ONCE,
            callback=lambda: None,
        )
    assert sink.started_count == 0


def test_handle_keeps_private_authority_snapshot_after_caller_drift() -> None:
    authority = _authority("snapshot")
    original_request_sha256 = authority.request_sha256
    runner, _ = _runner()
    handle = _begin(runner, authority, CpuFixedAttemptScriptV1.COMPLETE_ONCE)
    object.__setattr__(authority, "request_sha256", _digest("caller-drift"))

    receipt = handle.execute()

    assert receipt.request_sha256 == original_request_sha256
    assert receipt.request_sha256 != authority.request_sha256


def test_canonical_provider_request_and_pricing_are_exactly_hash_bound() -> None:
    source = {"model": "gpt-5.6-sol", "input": [{"text": "fixture"}], "store": False}
    request = build_canonical_history_policy_request(source)
    source["model"] = "caller-drift"

    assert request.canonical_bytes.startswith(b'{"input"')
    assert b"caller-drift" not in request.canonical_bytes
    assert request.byte_count == len(request.canonical_bytes)
    with pytest.raises(LiveAttemptError) as drift:
        replace(request, request_sha256=_digest("drift"))
    assert drift.value.code == "REQUEST_HASH_DRIFT"
    with pytest.raises(LiveAttemptError) as forged:
        replace(request, _seal=object())
    assert forged.value.code == "UNTRUSTED_PROVIDER_REQUEST"

    pricing = LiveAttemptPricingV1(
        pricing_id="owner-pin-2026-09-03",
        model="gpt-5.6-sol",
        input_usd_micros_per_million_tokens=1_000_000,
        cached_input_usd_micros_per_million_tokens=100_000,
        output_usd_micros_per_million_tokens=2_000_000,
        source_sha256=_digest("pricing-source"),
        effective_at_utc="2026-09-03T00:00:00Z",
    )
    assert len(live_attempt_pricing_sha256(pricing)) == 64
    assert (
        live_attempt_cost_usd_micros(
            pricing,
            input_tokens=1_000_000,
            cached_input_tokens=400_000,
            output_tokens=100_000,
        )
        == 840_000
    )
    with pytest.raises(LiveAttemptError) as invalid_cached:
        live_attempt_cost_usd_micros(
            pricing,
            input_tokens=1,
            cached_input_tokens=2,
            output_tokens=0,
        )
    assert invalid_cached.value.code == "INVALID_TOKEN_CENSUS"


def test_production_runner_rejects_every_non_post_preflight_factory() -> None:
    assert production_live_attempt_runner_available_v1()
    pricing = LiveAttemptPricingV1(
        pricing_id="owner-pin-2026-09-03",
        model="gpt-5.6-sol",
        input_usd_micros_per_million_tokens=1_000_000,
        cached_input_usd_micros_per_million_tokens=100_000,
        output_usd_micros_per_million_tokens=2_000_000,
        source_sha256=_digest("pricing-source"),
        effective_at_utc="2026-09-03T00:00:00Z",
    )
    sink = MemoryLiveAttemptReceiptSinkV1()
    with pytest.raises(LiveAttemptError) as raised:
        ProductionHistoryPolicyAttemptRunnerV1(
            factory=cast(Any, object()),
            role=LiveAttemptRoleV1.HISTORY_POLICY,
            sink=sink,
            pricing=pricing,
            confirmed_pricing_sha256=live_attempt_pricing_sha256(pricing),
        )
    assert raised.value.code == "UNTRUSTED_PRODUCTION_FACTORY"
    assert sink.started_count == sink.terminal_count == 0


def test_seam_fence_cancels_and_reaps_the_exact_process_call() -> None:
    authority = _authority("seam-cancel")
    sink = MemoryLiveAttemptReceiptSinkV1()
    runner = CpuFixedCancellableAttemptRunnerV1(sink=sink)
    call = runner.begin(
        authority,
        confirmed_authority_sha256=live_attempt_authority_sha256(authority),
        script=CpuFixedAttemptScriptV1.BLOCK_AFTER_DISPATCH,
    )
    fence = _PolicyExecutionFence(
        deadline_ns=time.monotonic_ns() + 5_000_000_000,
        clock_ns=time.monotonic_ns,
    )
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            fence.run_transport(call)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 2
    while call.dispatch_count == 0 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert call.dispatch_count == 1

    fence.cancel()
    worker.join(2)

    assert not worker.is_alive()
    assert len(failures) == 1
    receipt = call.terminal_receipt
    assert receipt is not None
    assert receipt.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
    assert receipt.execution_kind.value == "CPU_FIXED_SUBPROCESS"
    assert receipt.worker_reaped
    assert sink.receipts == (receipt,)
