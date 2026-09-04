from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest

from mobile_world.runtime.sentinel.r2_4 import (
    production_preflight as production_preflight_module,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1,
    PRODUCTION_ATTEMPT_KILL_REAP_WAIT_MS_V1,
    PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1,
    CpuFixedAttemptScriptV1,
    CpuFixedCancellableAttemptRunnerV1,
    CpuFixedLiveAttemptHandleV1,
    CpuFixedLiveAttemptRunnerV1,
    LiveAttemptAuthorityV1,
    LiveAttemptCostStatusV1,
    LiveAttemptError,
    LiveAttemptExecutionKindV1,
    LiveAttemptPricingV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    MemoryLiveAttemptReceiptSinkV1,
    ProductionHistoryPolicyAttemptRunnerV1,
    ProductionOpenAIAttemptCallV1,
    build_canonical_history_policy_request,
    live_attempt_authority_projection,
    live_attempt_authority_sha256,
    live_attempt_cost_usd_micros,
    live_attempt_pricing_sha256,
    live_attempt_receipt_projection,
    live_attempt_receipt_root_sha256,
    live_attempt_receipt_sha256,
    production_attempt_termination_upper_bound_ns_v1,
    production_live_attempt_runner_available_v1,
    snapshot_live_attempt_authority,
    snapshot_live_attempt_receipt,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    openai_stage_set_sha256,
    openai_stage_sha256,
)
from mobile_world.runtime.sentinel.seam import _PolicyExecutionFence


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def test_production_termination_bound_matches_sealed_term_and_kill_waits() -> None:
    expected_ns = (
        2 * PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1
        + max(
            PRODUCTION_ATTEMPT_CANCEL_GRACE_MS_V1,
            PRODUCTION_ATTEMPT_KILL_REAP_WAIT_MS_V1,
        )
    ) * 1_000_000
    assert production_attempt_termination_upper_bound_ns_v1() == expected_ns
    assert PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1 == expected_ns
    assert expected_ns == 7_000_000_000


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


def _pricing() -> LiveAttemptPricingV1:
    return LiveAttemptPricingV1(
        pricing_id="owner-pin-2026-09-03",
        model="gpt-5.6-sol",
        input_usd_micros_per_million_tokens=1_000_000,
        cached_input_usd_micros_per_million_tokens=100_000,
        output_usd_micros_per_million_tokens=2_000_000,
        source_sha256=_digest("pricing-source"),
        effective_at_utc="2026-09-03T00:00:00Z",
    )


def _stage(role: LiveAttemptRoleV1) -> OpenAIResponsesStageV1:
    openai_role = (
        OpenAIRoleV1.RUBRIC if role is LiveAttemptRoleV1.RUBRIC else OpenAIRoleV1.HISTORY_POLICY
    )
    return OpenAIResponsesStageV1(
        role=openai_role,
        model="gpt-5.6-sol",
        endpoint="https://api.openai.com/v1/responses",
        transport_kind="OPENAI_RESPONSES",
        transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
        openai_sdk_version="1.106.1",
        sdk_max_retries=0,
        external_network_on_call=True,
        model_on_call=True,
        max_output_tokens=8192 if role is LiveAttemptRoleV1.RUBRIC else 4096,
        timeout_ms=60_000,
        max_attempts=1,
        store=False,
    )


def _history_policy_request_kwargs() -> dict[str, object]:
    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
        GPT56_OUTPUT_SCHEMA_NAME,
        GPT56_POLICY_INSTRUCTIONS,
        GPT56_REASONING_EFFORT,
        ProposalSchemaSnapshotV1,
    )

    return {
        "model": "gpt-5.6-sol",
        "instructions": GPT56_POLICY_INSTRUCTIONS,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "{}"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,AA==",
                        "detail": "high",
                    },
                ],
            }
        ],
        "reasoning": {"effort": GPT56_REASONING_EFFORT},
        "text": {
            "format": {
                "type": "json_schema",
                "name": GPT56_OUTPUT_SCHEMA_NAME,
                "strict": True,
                "schema": ProposalSchemaSnapshotV1.from_checked_in().as_dict(),
            },
            "verbosity": "low",
        },
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "store": False,
        "stream": False,
        "truncation": "disabled",
        "max_output_tokens": 4096,
    }


def _rubric_request_kwargs(*, track: bool) -> dict[str, object]:
    from mobile_world.runtime.sentinel.r2_4.rubric_live import (
        _GENERATE_INSTRUCTIONS,
        _TRACK_INSTRUCTIONS,
        LIVE_RUBRIC_REASONING_EFFORT,
        live_rubric_generate_schema,
        live_rubric_track_schema,
    )

    schema = live_rubric_track_schema() if track else live_rubric_generate_schema()
    content: list[dict[str, object]] = [{"type": "input_text", "text": "{}"}]
    if track:
        content.append(
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,AA==",
                "detail": "high",
            }
        )
    return {
        "model": "gpt-5.6-sol",
        "instructions": _TRACK_INSTRUCTIONS if track else _GENERATE_INSTRUCTIONS,
        "input": [{"role": "user", "content": content}],
        "reasoning": {"effort": LIVE_RUBRIC_REASONING_EFFORT},
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema.name,
                "strict": True,
                "schema": schema.as_dict(),
            },
            "verbosity": "low",
        },
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "store": False,
        "stream": False,
        "truncation": "disabled",
        "max_output_tokens": 8192,
    }


def _replace_first_integer_one_with_boolean_true(value: object) -> bool:
    if type(value) is dict:
        for key, child in value.items():
            if key == "minItems" and type(child) is int and child == 1:
                value[key] = True
                return True
            if _replace_first_integer_one_with_boolean_true(child):
                return True
    elif type(value) is list:
        for child in value:
            if _replace_first_integer_one_with_boolean_true(child):
                return True
    return False


class _MemoryConnection:
    def __init__(self, incoming: tuple[object, ...] | None = None) -> None:
        self.sent: list[tuple[object, ...]] = []
        self._incoming = incoming
        self.closed = False

    def send(self, value: tuple[object, ...]) -> None:
        self.sent.append(value)

    def recv(self) -> tuple[object, ...]:
        if self._incoming is None:
            raise EOFError
        value = self._incoming
        self._incoming = None
        return value

    def poll(self, timeout_seconds: float = 0.0) -> bool:
        del timeout_seconds
        return self._incoming is not None

    def close(self) -> None:
        self.closed = True

    def queue(self, incoming: tuple[object, ...]) -> None:
        assert self._incoming is None
        self._incoming = incoming


class _TerminatingProcess:
    pid = 4242

    def __init__(self, *, stoppable: bool = True, exited: bool = False) -> None:
        self._alive = not exited
        self._stoppable = stoppable
        self.exitcode: int | None = 0 if exited else None

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout_seconds: float = 0.0) -> None:
        del timeout_seconds

    def terminate(self) -> None:
        if self._stoppable:
            self._alive = False
            self.exitcode = -15

    def kill(self) -> None:
        if self._stoppable:
            self._alive = False
            self.exitcode = -9


def _production_call_for_request(
    request_kwargs: dict[str, object],
    *,
    process: _TerminatingProcess,
) -> tuple[ProductionOpenAIAttemptCallV1, MemoryLiveAttemptReceiptSinkV1, _MemoryConnection]:
    request = build_canonical_history_policy_request(request_kwargs)
    stage = _stage(LiveAttemptRoleV1.HISTORY_POLICY)
    authority = replace(
        _authority("sealed-request"),
        request_sha256=request.request_sha256,
        stage_sha256=openai_stage_sha256(stage),
        max_output_tokens=stage.max_output_tokens,
    )
    sink = MemoryLiveAttemptReceiptSinkV1()
    sink._reserve(authority)
    connection = _MemoryConnection()
    call = ProductionOpenAIAttemptCallV1(
        authority=authority,
        sink=sink,
        pricing=_pricing(),
        process=cast(Any, process),
        connection=cast(Any, connection),
        started_ns=time.monotonic_ns(),
        cancel_grace_seconds=0.001,
        execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
        request=request,
        stage=stage,
    )
    return call, sink, connection


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


def test_role_specific_sealed_requests_accept_only_exact_stage_config_and_schema() -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_attempt as live_attempt_module

    cases = (
        (
            LiveAttemptRoleV1.HISTORY_POLICY,
            _history_policy_request_kwargs(),
        ),
        (LiveAttemptRoleV1.RUBRIC, _rubric_request_kwargs(track=False)),
        (LiveAttemptRoleV1.RUBRIC, _rubric_request_kwargs(track=True)),
    )
    for role, kwargs in cases:
        request = build_canonical_history_policy_request(kwargs)
        validated = live_attempt_module._validate_sealed_provider_request(
            request.canonical_bytes,
            stage=_stage(role),
            role=role,
        )
        assert validated["model"] == "gpt-5.6-sol"
        assert validated["store"] is False
        assert validated["max_output_tokens"] == _stage(role).max_output_tokens


def test_sealed_schema_comparison_distinguishes_json_integer_from_boolean() -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_attempt as live_attempt_module

    expected = _history_policy_request_kwargs()
    drifted = cast(dict[str, object], json.loads(json.dumps(expected)))
    drifted_text = cast(dict[str, object], drifted["text"])
    drifted_format = cast(dict[str, object], drifted_text["format"])
    drifted_schema = cast(dict[str, object], drifted_format["schema"])
    expected_text = cast(dict[str, object], expected["text"])
    expected_format = cast(dict[str, object], expected_text["format"])
    expected_schema = cast(dict[str, object], expected_format["schema"])

    assert _replace_first_integer_one_with_boolean_true(drifted_schema)
    assert drifted_schema == expected_schema  # Python deliberately conflates 1 and True.
    assert json.dumps(drifted_schema, sort_keys=True) != json.dumps(expected_schema, sort_keys=True)

    request = build_canonical_history_policy_request(drifted)
    with pytest.raises(LiveAttemptError) as raised:
        live_attempt_module._validate_sealed_provider_request(
            request.canonical_bytes,
            stage=_stage(LiveAttemptRoleV1.HISTORY_POLICY),
            role=LiveAttemptRoleV1.HISTORY_POLICY,
        )
    assert raised.value.code == "PROVIDER_REQUEST_STAGE_MISMATCH"


def test_parent_rejects_request_drift_before_child_command_with_exact_zero_dispatch() -> None:
    base = _history_policy_request_kwargs()
    drifts: list[dict[str, object]] = []
    for field, value in (
        ("model", "other-model"),
        ("store", True),
        ("max_output_tokens", 8192),
    ):
        drift = cast(dict[str, object], json.loads(json.dumps(base)))
        drift[field] = value
        drifts.append(drift)
    schema_drift = cast(dict[str, object], json.loads(json.dumps(base)))
    text = cast(dict[str, object], schema_drift["text"])
    output_format = cast(dict[str, object], text["format"])
    output_schema = cast(dict[str, object], output_format["schema"])
    output_schema["title"] = "caller-drift"
    drifts.append(schema_drift)
    schema_type_confusion = cast(dict[str, object], json.loads(json.dumps(base)))
    type_confusion_text = cast(dict[str, object], schema_type_confusion["text"])
    type_confusion_format = cast(dict[str, object], type_confusion_text["format"])
    type_confusion_schema = cast(dict[str, object], type_confusion_format["schema"])
    assert _replace_first_integer_one_with_boolean_true(type_confusion_schema)
    drifts.append(schema_type_confusion)
    envelope_drift = cast(dict[str, object], json.loads(json.dumps(base)))
    input_value = cast(list[object], envelope_drift["input"])
    input_message = cast(dict[str, object], input_value[0])
    input_message["role"] = "system"
    drifts.append(envelope_drift)

    for request_kwargs in drifts:
        call, sink, connection = _production_call_for_request(
            request_kwargs,
            process=_TerminatingProcess(),
        )

        with pytest.raises(LiveAttemptError) as raised:
            call()

        assert raised.value.code == "PROVIDER_REQUEST_STAGE_MISMATCH"
        receipt = call.terminal_receipt
        assert receipt is not None
        assert receipt.status is LiveAttemptStatusV1.FAILED
        assert receipt.failure_code == "PROVIDER_REQUEST_STAGE_MISMATCH"
        assert receipt.dispatch_count == 0
        assert receipt.cost_status is LiveAttemptCostStatusV1.EXACT
        assert receipt.cost_usd_micros == 0
        assert receipt.worker_reaped
        assert connection.sent == []
        assert sink.receipts == (receipt,)


def test_child_revalidates_before_secret_read_or_provider_dispatch() -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_attempt as live_attempt_module

    stage = _stage(LiveAttemptRoleV1.HISTORY_POLICY)
    authority_sha256 = _digest("child-authority")
    connection = _MemoryConnection(("DISPATCH", authority_sha256))
    secret_reads: list[bool] = []

    class _Factory:
        def openai_stage(self, role: OpenAIRoleV1) -> OpenAIResponsesStageV1:
            assert role is OpenAIRoleV1.HISTORY_POLICY
            return stage

        def openai_stage_sha256(self, role: OpenAIRoleV1) -> str:
            assert role is OpenAIRoleV1.HISTORY_POLICY
            return openai_stage_sha256(stage)

        def _acquire_openai_secret_for_child_process(self, lease: object) -> object:
            del lease
            secret_reads.append(True)
            raise AssertionError("request drift must fail before secret read")

    drifted = build_canonical_history_policy_request(
        {"model": "gpt-5.6-sol", "input": [], "store": False}
    )
    live_attempt_module._production_openai_attempt_worker(
        cast(Any, connection),
        cast(Any, _Factory()),
        cast(Any, object()),
        drifted.canonical_bytes,
        authority_sha256,
        openai_stage_sha256(stage),
        LiveAttemptRoleV1.HISTORY_POLICY.value,
    )

    assert secret_reads == []
    assert connection.sent == [
        ("READY", authority_sha256),
        ("FAILED", authority_sha256, "PROVIDER_REQUEST_STAGE_MISMATCH"),
    ]
    assert connection.closed


def test_child_disables_env_driven_sdk_logs_before_client_and_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import openai

    from mobile_world.runtime.sentinel.r2_4 import live_attempt as live_attempt_module

    stage = _stage(LiveAttemptRoleV1.HISTORY_POLICY)
    authority_sha256 = _digest("child-sdk-log-suppression")
    connection = _MemoryConnection(("DISPATCH", authority_sha256))
    secret_token = "SYNTHETIC_CHILD_API_KEY_TOKEN_R24"
    task_token = "SYNTHETIC_CHILD_TASK_EVIDENCE_TOKEN_R24"
    image_token = "SYNTHETIC_CHILD_IMAGE_TOKEN_R24"
    observed_request_bodies: list[bytes] = []

    class _SecretLease:
        def close(self) -> None:
            return None

    class _Factory:
        def openai_stage(self, role: OpenAIRoleV1) -> OpenAIResponsesStageV1:
            assert role is OpenAIRoleV1.HISTORY_POLICY
            return stage

        def openai_stage_sha256(self, role: OpenAIRoleV1) -> str:
            assert role is OpenAIRoleV1.HISTORY_POLICY
            return openai_stage_sha256(stage)

        def _acquire_openai_secret_for_child_process(self, lease: object) -> tuple[object, str]:
            del lease
            assert "OPENAI_LOG" not in os.environ
            return _SecretLease(), secret_token

    def respond(request: httpx.Request) -> httpx.Response:
        observed_request_bodies.append(request.content)
        logging.getLogger("httpcore.connection").debug(
            "request headers and body: %s %s", dict(request.headers), request.content
        )
        return httpx.Response(
            500,
            request=request,
            json={"error": {"message": "cpu fake failure", "type": "cpu_fake"}},
        )

    def http_client_factory(**kwargs: object) -> httpx.Client:
        del kwargs
        assert "OPENAI_LOG" not in os.environ
        return httpx.Client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(openai, "DefaultHttpxClient", http_client_factory)
    monkeypatch.setenv("OPENAI_LOG", "debug")
    request_kwargs = _history_policy_request_kwargs()
    input_value = cast(list[dict[str, object]], request_kwargs["input"])
    content = cast(list[dict[str, object]], input_value[0]["content"])
    content[0]["text"] = task_token
    content[1]["image_url"] = f"data:image/png;base64,{image_token}"
    request = build_canonical_history_policy_request(request_kwargs)

    caplog.clear()
    with (
        caplog.at_level(logging.DEBUG, logger="openai._base_client"),
        caplog.at_level(logging.DEBUG, logger="httpcore.connection"),
    ):
        live_attempt_module._production_openai_attempt_worker(
            cast(Any, connection),
            cast(Any, _Factory()),
            cast(Any, object()),
            request.canonical_bytes,
            authority_sha256,
            openai_stage_sha256(stage),
            LiveAttemptRoleV1.HISTORY_POLICY.value,
        )
        logging.getLogger("openai._base_client").debug("NONPRODUCTION_CHILD_LOG_CANARY")

    assert os.environ["OPENAI_LOG"] == "debug"
    assert len(observed_request_bodies) == 1
    assert task_token.encode() in observed_request_bodies[0]
    assert image_token.encode() in observed_request_bodies[0]
    assert connection.sent == [
        ("READY", authority_sha256),
        ("DISPATCHED", authority_sha256),
        ("FAILED", authority_sha256, "PROVIDER_CHILD_FAILED"),
    ]
    assert "NONPRODUCTION_CHILD_LOG_CANARY" in caplog.text
    assert task_token not in caplog.text
    assert image_token not in caplog.text
    assert secret_token not in caplog.text
    assert connection.closed


@pytest.mark.parametrize(
    ("returned_model", "expected_kind"),
    (
        ("gpt-5.6-sol-snapshot", "PROVIDER_RETURNED_MODEL_MISMATCH"),
        (None, "PROVIDER_RETURNED_MODEL_INVALID"),
        (7, "PROVIDER_RETURNED_MODEL_INVALID"),
        ("unsafe\nmodel", "PROVIDER_RETURNED_MODEL_INVALID"),
        ("m" * 257, "PROVIDER_RETURNED_MODEL_INVALID"),
    ),
)
def test_child_projects_returned_model_before_r22_envelope_and_uses_closed_ipc(
    monkeypatch: pytest.MonkeyPatch,
    returned_model: object,
    expected_kind: str,
) -> None:
    import openai

    from mobile_world.runtime.sentinel.r2_2 import gpt56_policy
    from mobile_world.runtime.sentinel.r2_4 import live_attempt as live_attempt_module

    stage = _stage(LiveAttemptRoleV1.HISTORY_POLICY)
    authority_sha256 = _digest(f"child-returned-model-{expected_kind}")
    connection = _MemoryConnection(("DISPATCH", authority_sha256))
    projection_calls: list[bool] = []

    class _SecretLease:
        def close(self) -> None:
            return None

    class _Factory:
        def openai_stage(self, role: OpenAIRoleV1) -> OpenAIResponsesStageV1:
            assert role is OpenAIRoleV1.HISTORY_POLICY
            return stage

        def openai_stage_sha256(self, role: OpenAIRoleV1) -> str:
            assert role is OpenAIRoleV1.HISTORY_POLICY
            return openai_stage_sha256(stage)

        def _acquire_openai_secret_for_child_process(self, lease: object) -> tuple[object, str]:
            del lease
            return _SecretLease(), "fixture-key"

    class _RawResponse:
        pass

    raw = _RawResponse()
    if returned_model is not None:
        setattr(raw, "model", returned_model)

    class _Responses:
        def create(self, **kwargs: object) -> object:
            assert kwargs["model"] == stage.model
            return raw

    class _Client:
        responses = _Responses()

        def close(self) -> None:
            return None

    class _HttpClient:
        def close(self) -> None:
            return None

    monkeypatch.setattr(openai, "Timeout", lambda _: object())
    monkeypatch.setattr(openai, "DefaultHttpxClient", lambda **_: _HttpClient())
    monkeypatch.setattr(openai, "OpenAI", lambda **_: _Client())

    def _unexpected_projection(*_: object, **__: object) -> object:
        projection_calls.append(True)
        raise AssertionError("model drift must be classified before R2.2 envelope construction")

    monkeypatch.setattr(gpt56_policy, "_project_openai_response", _unexpected_projection)

    request = build_canonical_history_policy_request(_history_policy_request_kwargs())
    live_attempt_module._production_openai_attempt_worker(
        cast(Any, connection),
        cast(Any, _Factory()),
        cast(Any, object()),
        request.canonical_bytes,
        authority_sha256,
        openai_stage_sha256(stage),
        LiveAttemptRoleV1.HISTORY_POLICY.value,
    )

    assert projection_calls == []
    assert connection.sent[:2] == [
        ("READY", authority_sha256),
        ("DISPATCHED", authority_sha256),
    ]
    if expected_kind == "PROVIDER_RETURNED_MODEL_MISMATCH":
        assert connection.sent[2] == (expected_kind, authority_sha256, returned_model)
    else:
        assert connection.sent[2] == (expected_kind, authority_sha256)
        assert returned_model not in connection.sent[2]
    assert len(connection.sent) == 3
    assert connection.closed


@pytest.mark.parametrize(
    ("message_kind", "returned_model"),
    (
        ("PROVIDER_RETURNED_MODEL_MISMATCH", "gpt-5.6-sol-snapshot"),
        ("PROVIDER_RETURNED_MODEL_INVALID", None),
    ),
)
def test_parent_persists_closed_returned_model_failure_from_its_sealed_stage(
    message_kind: str,
    returned_model: str | None,
) -> None:
    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(exited=True),
    )
    if returned_model is None:
        connection.queue((message_kind, call.authority_sha256))
    else:
        connection.queue((message_kind, call.authority_sha256, returned_model))

    with pytest.raises(LiveAttemptError) as raised:
        call()

    assert raised.value.code == message_kind
    receipt = call.terminal_receipt
    assert receipt is not None
    assert receipt.status is LiveAttemptStatusV1.FAILED
    assert receipt.failure_code == message_kind
    assert receipt.dispatch_count == 1
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert receipt.cost_usd_micros is None
    assert receipt.requested_model == _stage(LiveAttemptRoleV1.HISTORY_POLICY).model
    assert receipt.returned_model == returned_model
    assert receipt.worker_reaped and receipt.worker_exit_code == 0
    assert connection.sent == [("DISPATCH", call.authority_sha256)]
    assert sink.receipts == (receipt,)
    projection = live_attempt_receipt_projection(receipt)
    assert projection["requested_model"] == "gpt-5.6-sol"
    assert projection["returned_model"] == returned_model
    assert snapshot_live_attempt_receipt(receipt) == receipt
    assert live_attempt_receipt_sha256(receipt) == live_attempt_receipt_sha256(
        snapshot_live_attempt_receipt(receipt)
    )


def test_parent_rejects_unsafe_model_ipc_without_persisting_provider_value() -> None:
    unsafe_model = "unsafe\nprovider-value"
    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(exited=True),
    )
    connection.queue(("PROVIDER_RETURNED_MODEL_MISMATCH", call.authority_sha256, unsafe_model))

    with pytest.raises(LiveAttemptError) as raised:
        call()

    assert raised.value.code == "PROVIDER_CHILD_PROTOCOL_VIOLATION"
    receipt = call.terminal_receipt
    assert receipt is not None
    assert receipt.failure_code == "PROVIDER_CHILD_PROTOCOL_VIOLATION"
    assert receipt.dispatch_count == 1
    assert receipt.requested_model is None
    assert receipt.returned_model is None
    assert unsafe_model not in json.dumps(live_attempt_receipt_projection(receipt))
    assert sink.receipts == (receipt,)


def test_production_completion_binds_requested_and_returned_stage_model() -> None:
    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import ResponsesEnvelopeV1

    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(exited=True),
    )
    envelope = ResponsesEnvelopeV1(
        response_id="resp-model-binding",
        requested_model="gpt-5.6-sol",
        returned_model="gpt-5.6-sol",
        status="completed",
        service_tier=None,
        output_text="{}",
        input_tokens=7,
        output_tokens=3,
        total_tokens=10,
    )
    connection.queue(("COMPLETED", call.authority_sha256, envelope, 0))

    assert call() == envelope
    receipt = call.terminal_receipt
    assert receipt is not None
    assert receipt.status is LiveAttemptStatusV1.COMPLETED
    assert receipt.requested_model == "gpt-5.6-sol"
    assert receipt.returned_model == receipt.requested_model
    assert sink.receipts == (receipt,)
    with pytest.raises(LiveAttemptError) as model_drift:
        replace(receipt, returned_model="gpt-5.6-sol-snapshot")
    assert model_drift.value.code == "INVALID_COMPLETED_RECEIPT"


def _retained_response_for_call(call: ProductionOpenAIAttemptCallV1) -> object | None:
    runner = object.__new__(ProductionHistoryPolicyAttemptRunnerV1)
    runner._constraint_lock = threading.Lock()
    runner._attempt_calls = {call.authority.attempt_id: call}
    return runner.response_envelope_for_attempt(call.authority.attempt_id)


def _completed_envelope(response_id: str) -> object:
    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import ResponsesEnvelopeV1

    return ResponsesEnvelopeV1(
        response_id=response_id,
        requested_model="gpt-5.6-sol",
        returned_model="gpt-5.6-sol",
        status="completed",
        service_tier=None,
        output_text="{}",
        input_tokens=7,
        output_tokens=3,
        total_tokens=10,
    )


def test_cancel_race_retains_late_completed_envelope_only_as_proof() -> None:
    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(),
    )
    envelope = _completed_envelope("resp-late-cancel")
    connection.queue(("COMPLETED", call.authority_sha256, envelope, 2))

    receipt = call.cancel_and_join()

    assert receipt.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
    assert receipt.dispatch_count == 1
    assert receipt.late_output_detected
    assert receipt.response_envelope_sha256 == cast(Any, envelope).sha256
    assert (receipt.input_tokens, receipt.cached_input_tokens, receipt.output_tokens) == (7, 2, 3)
    assert receipt.total_tokens == 10
    assert receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert receipt.cost_usd_micros == live_attempt_cost_usd_micros(
        _pricing(), input_tokens=7, cached_input_tokens=2, output_tokens=3
    )
    assert receipt.requested_model == receipt.returned_model == "gpt-5.6-sol"
    assert not receipt.passed
    retained = _retained_response_for_call(call)
    assert retained == envelope and retained is not envelope
    object.__setattr__(cast(Any, retained), "output_text", '{"caller":"drift"}')
    assert _retained_response_for_call(call) == envelope
    assert live_attempt_receipt_sha256(receipt) == live_attempt_receipt_sha256(
        snapshot_live_attempt_receipt(receipt)
    )
    assert sink.receipts == (receipt,)
    with pytest.raises(LiveAttemptError) as unavailable_to_policy:
        call()
    assert unavailable_to_policy.value.code == "LIVE_ATTEMPT_CANCELLED"


def test_cancel_after_natural_exit_with_dispatched_signal_is_terminal() -> None:
    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(exited=True),
    )
    connection.queue(("DISPATCHED", call.authority_sha256))

    receipt = call.cancel_and_join()

    assert receipt.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
    assert receipt.dispatch_count == 1
    assert receipt.termination is LiveAttemptTerminationV1.COOPERATIVE
    assert receipt.worker_reaped and receipt.worker_exit_code == 0
    assert receipt.cancellation_requested
    assert not receipt.late_output_detected
    assert receipt.response_envelope_sha256 is None
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert receipt.cost_usd_micros is None
    assert call.terminal_receipt == receipt
    assert sink.receipts == (receipt,)


def test_failed_race_retains_completion_already_removed_from_ipc_pipe() -> None:
    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(),
    )
    envelope = _completed_envelope("resp-late-failed")
    connection.queue(("COMPLETED", call.authority_sha256, envelope, 1))

    received = call._receive(0.0)
    assert received is not None
    receipt = call._failed("PROVIDER_CHILD_PROTOCOL_VIOLATION")

    assert receipt.status is LiveAttemptStatusV1.FAILED
    assert receipt.failure_code == "PROVIDER_CHILD_PROTOCOL_VIOLATION"
    assert receipt.dispatch_count == 1
    assert receipt.late_output_detected
    assert receipt.response_envelope_sha256 == cast(Any, envelope).sha256
    assert (receipt.input_tokens, receipt.cached_input_tokens, receipt.output_tokens) == (7, 1, 3)
    assert receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert receipt.cost_usd_micros == live_attempt_cost_usd_micros(
        _pricing(), input_tokens=7, cached_input_tokens=1, output_tokens=3
    )
    assert not receipt.passed
    assert _retained_response_for_call(call) == envelope
    assert sink.receipts == (receipt,)
    with pytest.raises(LiveAttemptError) as unavailable_to_policy:
        call()
    assert unavailable_to_policy.value.code == "PROVIDER_CHILD_DISPATCH_FAILED"
    assert sink.receipts == (receipt,)


def test_unreaped_cancel_race_retains_late_completion_with_exact_cost() -> None:
    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(stoppable=False),
    )
    envelope = _completed_envelope("resp-late-unreaped")
    connection.queue(("COMPLETED", call.authority_sha256, envelope, 2))

    receipt = call.cancel_and_join()

    assert receipt.status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
    assert receipt.failure_code == "TERMINATION_UNCONFIRMED"
    assert receipt.termination is LiveAttemptTerminationV1.UNCONFIRMED
    assert not receipt.worker_reaped
    assert receipt.late_output_detected
    assert receipt.response_envelope_sha256 == cast(Any, envelope).sha256
    assert (receipt.input_tokens, receipt.cached_input_tokens, receipt.output_tokens) == (7, 2, 3)
    assert receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert receipt.cost_usd_micros == live_attempt_cost_usd_micros(
        _pricing(), input_tokens=7, cached_input_tokens=2, output_tokens=3
    )
    assert not receipt.passed
    assert _retained_response_for_call(call) == envelope
    assert sink.receipts == (receipt,)
    with pytest.raises(LiveAttemptError) as unavailable_to_policy:
        call()
    assert unavailable_to_policy.value.code == "LIVE_ATTEMPT_CANCELLED"


def test_malformed_late_completion_is_not_retained_or_serialized() -> None:
    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(),
    )
    unsafe_payload = "UNSAFE_LATE_PROVIDER_PAYLOAD"
    connection.queue(
        (
            "COMPLETED",
            call.authority_sha256,
            {"untrusted_response": unsafe_payload},
            0,
        )
    )

    receipt = call.cancel_and_join()

    assert receipt.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
    assert receipt.dispatch_count == 1
    assert receipt.late_output_detected
    assert receipt.response_envelope_sha256 is None
    assert receipt.requested_model is None and receipt.returned_model is None
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert _retained_response_for_call(call) is None
    assert unsafe_payload not in json.dumps(live_attempt_receipt_projection(receipt))
    assert sink.receipts == (receipt,)


def test_duplicate_conflicting_late_completions_fail_closed_without_response() -> None:
    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(),
    )
    first = _completed_envelope("resp-late-first")
    second = _completed_envelope("resp-late-second")
    connection.queue(("COMPLETED", call.authority_sha256, first, 0))
    assert call._receive(0.0) is not None
    connection.queue(("COMPLETED", call.authority_sha256, second, 0))

    receipt = call.cancel_and_join()

    assert receipt.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
    assert receipt.late_output_detected
    assert receipt.response_envelope_sha256 is None
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert _retained_response_for_call(call) is None
    assert sink.receipts == (receipt,)


def test_usage_missing_terminal_retains_observed_provider_envelope() -> None:
    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import ResponsesEnvelopeV1

    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(exited=True),
    )
    envelope = ResponsesEnvelopeV1(
        response_id="resp-usage-missing",
        requested_model="gpt-5.6-sol",
        returned_model="gpt-5.6-sol",
        status="completed",
        service_tier=None,
        output_text="{}",
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
    )
    connection.queue(("COMPLETED", call.authority_sha256, envelope, 0))

    with pytest.raises(LiveAttemptError) as raised:
        call()

    assert raised.value.code == "PROVIDER_USAGE_MISSING"
    receipt = call.terminal_receipt
    assert receipt is not None
    assert receipt.status is LiveAttemptStatusV1.FAILED
    assert receipt.response_envelope_sha256 == envelope.sha256
    assert receipt.requested_model == receipt.returned_model == "gpt-5.6-sol"
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert all(
        value is None
        for value in (
            receipt.input_tokens,
            receipt.cached_input_tokens,
            receipt.output_tokens,
            receipt.total_tokens,
        )
    )
    assert _retained_response_for_call(call) == envelope
    assert sink.receipts == (receipt,)


def test_completed_envelope_then_unreaped_worker_retains_response_in_tu() -> None:
    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import ResponsesEnvelopeV1

    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(stoppable=False),
    )
    envelope = ResponsesEnvelopeV1(
        response_id="resp-unreaped-after-complete",
        requested_model="gpt-5.6-sol",
        returned_model="gpt-5.6-sol",
        status="completed",
        service_tier=None,
        output_text="{}",
        input_tokens=7,
        output_tokens=3,
        total_tokens=10,
    )
    connection.queue(("COMPLETED", call.authority_sha256, envelope, 2))

    with pytest.raises(LiveAttemptError) as raised:
        call()

    assert raised.value.code == "TERMINATION_UNCONFIRMED"
    receipt = call.terminal_receipt
    assert receipt is not None
    assert receipt.status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
    assert receipt.response_envelope_sha256 == envelope.sha256
    assert (receipt.input_tokens, receipt.cached_input_tokens, receipt.output_tokens) == (7, 2, 3)
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert receipt.termination is LiveAttemptTerminationV1.UNCONFIRMED
    assert not receipt.worker_reaped
    assert _retained_response_for_call(call) == envelope
    assert sink.receipts == (receipt,)


def test_over_authority_terminal_retains_exact_response_and_priced_cost() -> None:
    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import ResponsesEnvelopeV1

    call, sink, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(exited=True),
    )
    envelope = ResponsesEnvelopeV1(
        response_id="resp-over-authority",
        requested_model="gpt-5.6-sol",
        returned_model="gpt-5.6-sol",
        status="completed",
        service_tier=None,
        output_text="{}",
        input_tokens=7,
        output_tokens=5_000,
        total_tokens=5_007,
    )
    connection.queue(("COMPLETED", call.authority_sha256, envelope, 0))

    with pytest.raises(LiveAttemptError) as raised:
        call()

    assert raised.value.code == "PROVIDER_RESULT_EXCEEDS_AUTHORITY"
    receipt = call.terminal_receipt
    assert receipt is not None
    assert receipt.status is LiveAttemptStatusV1.FAILED
    assert receipt.response_envelope_sha256 == envelope.sha256
    assert receipt.output_tokens == 5_000
    assert receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert receipt.cost_usd_micros == live_attempt_cost_usd_micros(
        _pricing(),
        input_tokens=7,
        cached_input_tokens=0,
        output_tokens=5_000,
    )
    assert receipt.cost_usd_micros > call.authority.max_cost_usd_micros
    assert _retained_response_for_call(call) == envelope
    assert sink.receipts == (receipt,)


def test_receipt_model_provenance_rejects_cpu_and_state_forgery() -> None:
    runner, _ = _runner()
    cpu_receipt = _begin(
        runner,
        _authority("cpu-model-provenance"),
        CpuFixedAttemptScriptV1.COMPLETE_ONCE,
    ).execute()
    assert cpu_receipt.requested_model is None
    assert cpu_receipt.returned_model is None
    assert live_attempt_receipt_projection(cpu_receipt)["requested_model"] is None
    assert live_attempt_receipt_projection(cpu_receipt)["returned_model"] is None

    with pytest.raises(LiveAttemptError) as cpu_forgery:
        replace(
            cpu_receipt,
            requested_model="gpt-5.6-sol",
            returned_model="gpt-5.6-sol",
        )
    assert cpu_forgery.value.code == "INVALID_MODEL_PROVENANCE"

    call, _, connection = _production_call_for_request(
        _history_policy_request_kwargs(),
        process=_TerminatingProcess(exited=True),
    )
    connection.queue(
        (
            "PROVIDER_RETURNED_MODEL_MISMATCH",
            call.authority_sha256,
            "gpt-5.6-sol-snapshot",
        )
    )
    with pytest.raises(LiveAttemptError):
        call()
    mismatch = call.terminal_receipt
    assert mismatch is not None

    with pytest.raises(LiveAttemptError) as unrelated_failure:
        replace(mismatch, failure_code="PROVIDER_CHILD_FAILED")
    assert unrelated_failure.value.code == "INVALID_MODEL_PROVENANCE"
    with pytest.raises(LiveAttemptError) as equal_models:
        replace(mismatch, returned_model=mismatch.requested_model)
    assert equal_models.value.code == "INVALID_MODEL_PROVENANCE"
    with pytest.raises(LiveAttemptError) as zero_dispatch:
        replace(
            mismatch,
            dispatch_count=0,
            cost_status=LiveAttemptCostStatusV1.EXACT,
            cost_usd_micros=0,
        )
    assert zero_dispatch.value.code == "INVALID_MODEL_PROVENANCE"
    with pytest.raises(LiveAttemptError) as unsafe_model:
        replace(mismatch, returned_model="unsafe\nmodel")
    assert unsafe_model.value.code == "INVALID_MODEL_PROVENANCE"


def test_failed_path_with_unreaped_worker_is_termination_unconfirmed() -> None:
    call, sink, connection = _production_call_for_request(
        {"model": "gpt-5.6-sol", "input": [], "store": False},
        process=_TerminatingProcess(stoppable=False),
    )

    with pytest.raises(LiveAttemptError) as raised:
        call()

    assert raised.value.code == "PROVIDER_REQUEST_STAGE_MISMATCH"
    receipt = call.terminal_receipt
    assert receipt is not None
    assert receipt.status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
    assert receipt.failure_code == "TERMINATION_UNCONFIRMED"
    assert receipt.termination is LiveAttemptTerminationV1.UNCONFIRMED
    assert receipt.dispatch_count == 0
    assert receipt.cost_status is LiveAttemptCostStatusV1.UNKNOWN
    assert receipt.cost_usd_micros is None
    assert receipt.cancellation_requested
    assert not receipt.worker_reaped
    assert connection.sent == []
    assert sink.receipts == (receipt,)


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


def test_history_constraint_registration_is_one_shot_and_call_bound() -> None:
    runner = object.__new__(ProductionHistoryPolicyAttemptRunnerV1)
    runner._role = LiveAttemptRoleV1.HISTORY_POLICY
    runner._factory = cast(
        Any,
        type(
            "_Factory",
            (),
            {"validate_case_execution_lease": staticmethod(lambda lease: lease)},
        )(),
    )
    runner._constraint_lock = threading.Lock()
    runner._pending_history_constraints = {}
    runner._known_history_constraint_attempt_ids = set()
    runner._attempt_deadline_bindings = {}
    runner._attempt_requests = {}
    runner._attempt_calls = {}
    logical_call_id = "history-constraint-call-1"
    lease = production_preflight_module._restore_case_execution_lease(
        {
            "actor_call_index": 1,
            "case_id": "history-constraint-case-1",
            "execution_scope": "OWNER_AUTHORIZED_LIVE",
            "expires_at_utc": "2026-09-04T01:00:00Z",
            "factory_binding_sha256": _digest("history-constraint-factory"),
            "host": "QWEN3_VL",
            "issued_at_utc": "2026-09-04T00:00:00Z",
            "manifest_sha256": _digest("history-constraint-manifest"),
            "mode": "SHADOW",
            "openai_stage_set_sha256": _digest("history-constraint-stages"),
            "preflight_report_sha256": _digest("history-constraint-preflight"),
            "pricing_binding_sha256": _digest("history-constraint-pricing"),
            "request_sha256": _digest("history-constraint-request"),
            "reset_seed": None,
            "schema_version": "mobileworld.runtime.sentinel-r2.4-case-execution-lease/v1",
            "stage": "QWEN_LIVE_SMOKE",
            "task_id": "history-constraint-task-1",
            "task_parameters_sha256": None,
        }
    )
    registered_ns = time.monotonic_ns()
    case_deadline_ns = registered_ns + 2_000_000_000
    request_timeout_ns = 1_000_000_000
    runner.register_history_attempt_constraint(
        case_lease=lease,
        attempt_id="history-constraint-attempt-1",
        logical_call_id=logical_call_id,
        case_execution_deadline_monotonic_ns=case_deadline_ns,
        request_timeout_ns=request_timeout_ns,
        max_cost_usd_micros=10,
    )
    with pytest.raises(LiveAttemptError, match="already used"):
        runner.register_history_attempt_constraint(
            case_lease=lease,
            attempt_id="history-constraint-attempt-1",
            logical_call_id=logical_call_id,
            case_execution_deadline_monotonic_ns=case_deadline_ns,
            request_timeout_ns=request_timeout_ns,
            max_cost_usd_micros=10,
        )
    with pytest.raises(LiveAttemptError, match="differs from its one-shot"):
        runner._consume_history_attempt_constraint(
            lease=lease,
            attempt_id="history-constraint-attempt-1",
            logical_call_id="history-constraint-other-call",
            requested_call_deadline_monotonic_ns=(time.monotonic_ns() + request_timeout_ns),
            max_cost_usd_micros=10,
            begin_observed_monotonic_ns=time.monotonic_ns(),
        )
    assert not runner.discard_unformed_history_attempt_constraint(
        attempt_id="history-constraint-attempt-1",
        logical_call_id=logical_call_id,
    )
    with pytest.raises(LiveAttemptError, match="already used"):
        runner.register_history_attempt_constraint(
            case_lease=lease,
            attempt_id="history-constraint-attempt-1",
            logical_call_id=logical_call_id,
            case_execution_deadline_monotonic_ns=case_deadline_ns,
            request_timeout_ns=request_timeout_ns,
            max_cost_usd_micros=10,
        )
    second_deadline_ns = time.monotonic_ns() + 2_000_000_000
    runner.register_history_attempt_constraint(
        case_lease=lease,
        attempt_id="history-constraint-attempt-2",
        logical_call_id=logical_call_id,
        case_execution_deadline_monotonic_ns=second_deadline_ns,
        request_timeout_ns=request_timeout_ns,
        max_cost_usd_micros=10,
    )
    requested_issued_ns = time.monotonic_ns()
    requested_deadline_ns = requested_issued_ns + request_timeout_ns
    begin_ns = time.monotonic_ns()
    binding = runner._consume_history_attempt_constraint(
        lease=lease,
        attempt_id="history-constraint-attempt-2",
        logical_call_id=logical_call_id,
        requested_call_deadline_monotonic_ns=requested_deadline_ns,
        max_cost_usd_micros=10,
        begin_observed_monotonic_ns=begin_ns,
    )
    assert binding.requested_deadline_issued_monotonic_ns == requested_issued_ns
    assert binding.effective_deadline_monotonic_ns == min(requested_deadline_ns, second_deadline_ns)
    with pytest.raises(LiveAttemptError, match="no one-shot"):
        runner._consume_history_attempt_constraint(
            lease=lease,
            attempt_id="history-constraint-attempt-2",
            logical_call_id=logical_call_id,
            requested_call_deadline_monotonic_ns=requested_deadline_ns,
            max_cost_usd_micros=10,
            begin_observed_monotonic_ns=begin_ns,
        )
    with pytest.raises(LiveAttemptError):
        runner.register_history_attempt_constraint(
            case_lease=lease,
            attempt_id="history-constraint-bool",
            logical_call_id=logical_call_id,
            case_execution_deadline_monotonic_ns=cast(Any, True),
            request_timeout_ns=request_timeout_ns,
            max_cost_usd_micros=10,
        )


def test_history_begin_cost_reservation_failure_retains_formed_proof_preimages() -> None:
    request = build_canonical_history_policy_request(_history_policy_request_kwargs())
    history_stage = _stage(LiveAttemptRoleV1.HISTORY_POLICY)
    rubric_stage = _stage(LiveAttemptRoleV1.RUBRIC)
    pricing = _pricing()
    lease = production_preflight_module._restore_case_execution_lease(
        {
            "actor_call_index": 1,
            "case_id": "history-cost-failure-case-1",
            "execution_scope": "OWNER_AUTHORIZED_LIVE",
            "expires_at_utc": "2026-09-04T01:00:00Z",
            "factory_binding_sha256": _digest("history-cost-failure-factory"),
            "host": "QWEN3_VL",
            "issued_at_utc": "2026-09-04T00:00:00Z",
            "manifest_sha256": _digest("history-cost-failure-manifest"),
            "mode": "SHADOW",
            "openai_stage_set_sha256": openai_stage_set_sha256((rubric_stage, history_stage)),
            "preflight_report_sha256": _digest("history-cost-failure-preflight"),
            "pricing_binding_sha256": live_attempt_pricing_sha256(pricing),
            "request_sha256": _digest("history-cost-failure-actor-request"),
            "reset_seed": None,
            "schema_version": "mobileworld.runtime.sentinel-r2.4-case-execution-lease/v1",
            "stage": "QWEN_LIVE_SMOKE",
            "task_id": "history-cost-failure-task-1",
            "task_parameters_sha256": None,
        }
    )
    sink = MemoryLiveAttemptReceiptSinkV1()
    runner = object.__new__(ProductionHistoryPolicyAttemptRunnerV1)
    runner._role = LiveAttemptRoleV1.HISTORY_POLICY
    runner._factory = cast(
        Any,
        type(
            "_Factory",
            (),
            {
                "validate_case_execution_lease": staticmethod(lambda value: value),
                "openai_stage_sha256": staticmethod(
                    lambda _role: openai_stage_sha256(history_stage)
                ),
            },
        )(),
    )
    runner._stage = history_stage
    runner._pricing = pricing
    runner._pricing_sha256 = live_attempt_pricing_sha256(pricing)
    runner._sink = sink
    runner._constraint_lock = threading.Lock()
    runner._pending_history_constraints = {}
    runner._known_history_constraint_attempt_ids = set()
    runner._attempt_deadline_bindings = {}
    runner._attempt_requests = {}
    runner._attempt_calls = {}
    attempt_id = "history-cost-failure-attempt-1"
    logical_call_id = "history-cost-failure-call-1"
    now_ns = time.monotonic_ns()
    runner.register_history_attempt_constraint(
        case_lease=lease,
        attempt_id=attempt_id,
        logical_call_id=logical_call_id,
        case_execution_deadline_monotonic_ns=now_ns + 5_000_000_000,
        request_timeout_ns=1_000_000_000,
        max_cost_usd_micros=1,
    )

    with pytest.raises(LiveAttemptError) as raised:
        requested_deadline_ns = time.monotonic_ns() + 1_000_000_000
        runner.begin(
            case_lease=lease,
            attempt_id=attempt_id,
            logical_call_id=logical_call_id,
            request=request,
            transport_binding_sha256=_digest("history-cost-failure-transport"),
            deadline_monotonic_ns=requested_deadline_ns,
            max_cost_usd_micros=1,
        )

    assert raised.value.code == "ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY"
    receipt = runner.terminal_receipt_for_attempt(attempt_id)
    authority = runner.attempt_authority_for_attempt(attempt_id)
    assert receipt is not None and authority is not None
    assert receipt.status is LiveAttemptStatusV1.FAILED
    assert receipt.dispatch_count == 0
    assert receipt.authority_sha256 == live_attempt_authority_sha256(authority)
    assert runner.canonical_request_for_attempt(attempt_id) == request
    assert runner.attempt_deadline_binding_for_attempt(attempt_id) is not None
    assert runner.response_envelope_for_attempt(attempt_id) is None


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
