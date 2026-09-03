from __future__ import annotations

import json
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import Lock
from time import monotonic_ns
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import MockTransport, Request, Response
from openai import DefaultHttpxClient, OpenAI

from mobile_world.agents.base import BaseAgent
from mobile_world.offline.causal_replay.contracts import JsonValue, canonical_sha256
from mobile_world.runtime.audit.context import AuditContext, bind_audit_context
from mobile_world.runtime.audit.recorder import RunRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.sentinel import (
    MemorySentinelReceiptSink,
    PromptSentinel,
    SentinelFallbackReason,
    SentinelGlobalSwitch,
    SentinelHostConfig,
    SentinelMode,
    bind_sentinel_logical_call,
)
from mobile_world.runtime.sentinel.policies import NoOpSentinelPolicy
from mobile_world.runtime.sentinel.r2_4 import production_audit as production_audit_module
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    build_runtime_history_codec_resolver,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    RuntimeVerticalSentinelResultV1,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import MemoryLiveAttemptReceiptSinkV1
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    OwnerAuthorizedLivePerCallPolicyV1,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    ExternalProductionRuntimeAuditSinkV1,
    MemoryProductionRuntimeAuditSinkV1,
    ProductionRuntimeAuditCommitFailureReceiptV1,
    ProductionRuntimeAuditDetailV1,
    ProductionRuntimeAuditError,
    ProductionRuntimeAuditPreProviderStatusV1,
    ProductionRuntimeAuditPreProviderV1,
    ProductionRuntimeAuditPublicationStatusV1,
    ProductionRuntimeAuditSinkV1,
    ProductionRuntimeAuditTerminalKindV1,
    ProductionRuntimeAuditTransactionV1,
    ProductionRuntimeAuditV1,
    production_runtime_audit_commit_failure_receipt_projection,
    production_runtime_audit_detail_projection,
)
from mobile_world.runtime.utils.models import WAIT, JSONAction

QWEN_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "tests/offline/fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json"
)


class _Agent(BaseAgent):
    sentinel_host_id = "mobileworld.production-audit-test.actor"
    sentinel_history_codec_id = "mobileworld.g1.history-codec.qwen-flat-progress"

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        del observation
        raise NotImplementedError


class _FallbackAgent(BaseAgent):
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        del observation
        raise NotImplementedError


class _ActorResponse:
    def __init__(self, content: str) -> None:
        self.id = "actor-response-1"
        self.model = "cpu-fake-actor"
        self.usage = SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
        )
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ]
        self._content = content

    def model_dump(self, *, mode: str, exclude_none: bool = False) -> dict[str, Any]:
        assert mode == "json"
        assert not exclude_none
        return {
            "id": self.id,
            "model": self.model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": self._content,
                        "reasoning_content": "PRIVATE_PROVIDER_REASONING",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        }


class _CommitFaultTransaction:
    def __init__(self, pre_provider: ProductionRuntimeAuditPreProviderV1) -> None:
        self._logical_call_id = pre_provider.logical_call_id
        self._pre_provider_sha256 = (
            production_audit_module.production_runtime_audit_pre_provider_sha256(pre_provider)
        )
        self.attempted_detail: ProductionRuntimeAuditDetailV1 | None = None
        self.attempted_failure_detail: JsonValue | None = None
        self.abort_count = 0

    @property
    def logical_call_id(self) -> str:
        return self._logical_call_id

    @property
    def pre_provider_sha256(self) -> str:
        return self._pre_provider_sha256

    def commit(self, detail: ProductionRuntimeAuditDetailV1) -> None:
        self.attempted_detail = detail
        raise OSError("injected terminal commit fault")

    def commit_failure(self, detail: JsonValue) -> None:
        self.attempted_failure_detail = deepcopy(detail)
        raise OSError("injected failed-terminal commit fault")

    def abort(self) -> None:
        self.abort_count += 1


class _CommitFaultSink:
    def __init__(self) -> None:
        self.transactions: list[_CommitFaultTransaction] = []

    def begin(
        self, pre_provider: ProductionRuntimeAuditPreProviderV1
    ) -> ProductionRuntimeAuditTransactionV1:
        transaction = _CommitFaultTransaction(pre_provider)
        self.transactions.append(transaction)
        return transaction


def _collector_context(tmp_path: Path) -> tuple[RunRecorder, AuditContext]:
    run = RunRecorder(
        tmp_path / "collector",
        producer=Producer.local(version="r2.4-test", worker_id="production-audit"),
        sync=False,
    )
    run.write_manifest_start({"run_id": run.run_id})
    task = run.open_task()
    capture = RunnerTaskCapture(task)
    assert (
        capture.start_task(
            task_name="ProductionAudit",
            task_goal="Wait on the current screen.",
            task_goal_status="resolved",
            task_index=1,
            suite_family="mobile_world",
            agent={"adapter": "cpu-test", "model": "cpu-fake", "configuration": {}},
            environment={"backend_id": "cpu-fixture", "device_id": "none"},
            whole_task_attempt_index=1,
        )
        is not None
    )
    step = capture.start_step(
        step_index=1,
        observation={
            "screenshot": None,
            "accessibility_tree": {"screen": "current"},
            "tool_call": None,
            "ask_user_response": None,
        },
    )
    assert step is not None
    return run, AuditContext(
        run_id=run.run_id,
        recorder=task,
        task_run_id=task.task_run_id,
        step_id=step.step_id,
        decision_id=step.decision_id,
        parent_event_id=step.step_started_event_id,
    )


def _off_agent(
    sink: ProductionRuntimeAuditSinkV1,
) -> tuple[_Agent, ProductionRuntimeAuditV1, list[dict[str, Any]]]:
    audit = ProductionRuntimeAuditV1(policy=None, sink=sink)
    sentinel = PromptSentinel(
        policy=NoOpSentinelPolicy(),
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={_Agent.sentinel_host_id: SentinelHostConfig(mode=SentinelMode.OFF)},
        receipt_sink=MemorySentinelReceiptSink(),
        runtime_audit=audit,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: "production-audit-off-call-1",
    )
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> _ActorResponse:
        calls.append(kwargs)
        return _ActorResponse(
            "PRIVATE_PROVIDER_OUTPUT "
            '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
        )

    agent = _Agent(prompt_sentinel=sentinel)
    agent.openai_client = SimpleNamespace(
        base_url="http://127.0.0.1:1/v1",
        max_retries=0,
        timeout=1.0,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    return agent, audit, calls


def _unstarted_exact_live_policy() -> OwnerAuthorizedLivePerCallPolicyV1:
    """Build no executable authority; tests only a pre-policy typed fallback."""

    policy = object.__new__(OwnerAuthorizedLivePerCallPolicyV1)
    policy._policy_id = "r24-production-audit-unstarted-live-policy"
    policy._lock = Lock()
    policy._call_inputs = {}
    policy._bindings = {}
    policy._failures = {}
    return policy


def _run_off_call(
    tmp_path: Path,
    sink: ProductionRuntimeAuditSinkV1,
) -> tuple[ProductionRuntimeAuditV1, list[dict[str, Any]]]:
    run, context = _collector_context(tmp_path)
    agent, audit, calls = _off_agent(sink)
    action = JSONAction(action_type=WAIT)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                temperature=0.0,
            )
            assert type(prediction) is str
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            receipt = agent.finalize_prompt_sentinel_action_execution(
                action=action,
                action_executed=False,
            )
            assert receipt is not None
    finally:
        run.close()
    return audit, calls


@pytest.mark.parametrize(
    ("stage_shape", "action_executed"),
    (("SMOKE_ACTOR_SUCCESS", False), ("PILOT_ACTION_EXECUTED", True)),
)
def test_terminal_commit_fault_retains_recoverable_actor_action_and_cost_preimage(
    tmp_path: Path,
    stage_shape: str,
    action_executed: bool,
) -> None:
    sink = _CommitFaultSink()
    run, context = _collector_context(tmp_path)
    agent, audit, calls = _off_agent(sink)
    action = JSONAction(action_type=WAIT)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": f"{stage_shape}: wait now."}],
                temperature=0.0,
            )
            assert type(prediction) is str
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            with pytest.raises(ProductionRuntimeAuditError) as raised:
                agent.finalize_prompt_sentinel_action_execution(
                    action=action,
                    action_executed=action_executed,
                    action_execution_ns=11 if action_executed else 0,
                )
    finally:
        run.close()

    assert raised.value.code == "AUDIT_TERMINAL_COMMIT_FAILED"
    assert len(calls) == 1
    assert audit.pending_count == 0
    assert audit.latest_completed_receipt is None
    assert audit.latest_failure_receipt is None
    recovery = audit.latest_commit_failure_receipt
    assert type(recovery) is ProductionRuntimeAuditCommitFailureReceiptV1
    assert recovery is audit.commit_failure_receipt_for(recovery.logical_call_id)
    assert recovery.terminal_kind is ProductionRuntimeAuditTerminalKindV1.ACTION_EXECUTION
    assert (
        recovery.publication_status
        is ProductionRuntimeAuditPublicationStatusV1.COMMIT_OUTCOME_UNKNOWN
    )
    projection = production_runtime_audit_commit_failure_receipt_projection(recovery)
    terminal = cast(dict[str, JsonValue], projection["attempted_terminal_receipt"])
    pre_provider = cast(dict[str, JsonValue], projection["pre_provider"])
    assert canonical_sha256(cast(JsonValue, pre_provider)) == terminal["pre_provider_sha256"]
    assert terminal["action_executed"] is action_executed
    assert (terminal["executed_action_sha256"] is not None) is action_executed
    assert terminal["provider_attempt_count"] == 1
    assert terminal["live_openai_calls"] == 0
    assert terminal["live_cost_usd_micros"] == 0
    assert terminal["live_cost_exact"] is True
    assert cast(dict[str, JsonValue], projection["parsed_action"])["action_type"] == WAIT
    attempts = cast(list[dict[str, JsonValue]], projection["actor_provider_attempts"])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "SUCCEEDED"
    assert attempts[0]["collector_terminal_locator"]["event_type"] == "model_response"
    assert sink.transactions[0].attempted_detail is not None
    assert sink.transactions[0].abort_count == 1


def test_failed_actor_terminal_commit_fault_retains_negative_receipt(tmp_path: Path) -> None:
    sink = _CommitFaultSink()
    agent, audit, calls = _off_agent(sink)
    sentinel = agent._prompt_sentinel
    logical_call = sentinel.logical_call(
        host_id=_Agent.sentinel_host_id,
        history_codec_id=_Agent.sentinel_history_codec_id,
        attributes={"r24_case_deadline_monotonic_ns": 1},
    )
    run, context = _collector_context(tmp_path)
    try:
        with bind_audit_context(context), bind_sentinel_logical_call(logical_call):
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=1,
            )
            assert prediction is None
            with pytest.raises(ProductionRuntimeAuditError) as raised:
                agent._finalize_prompt_sentinel_actor_failure(
                    logical_call,
                    failure_phase="ACTOR_DEADLINE",
                    failure_code="ACTOR_CASE_DEADLINE_ELAPSED",
                )
    finally:
        run.close()

    assert raised.value.code == "AUDIT_TERMINAL_COMMIT_FAILED"
    assert calls == []
    assert audit.latest_failure_receipt is None
    recovery = audit.latest_commit_failure_receipt
    assert type(recovery) is ProductionRuntimeAuditCommitFailureReceiptV1
    assert recovery.terminal_kind is ProductionRuntimeAuditTerminalKindV1.ACTOR_FAILURE
    projection = production_runtime_audit_commit_failure_receipt_projection(recovery)
    terminal = cast(dict[str, JsonValue], projection["attempted_terminal_receipt"])
    pre_provider = cast(dict[str, JsonValue], projection["pre_provider"])
    assert canonical_sha256(cast(JsonValue, pre_provider)) == terminal["pre_provider_sha256"]
    assert terminal["failure_code"] == "ACTOR_CASE_DEADLINE_ELAPSED"
    assert terminal["provider_attempt_count"] == 0
    assert projection["actor_provider_attempts"] == []
    assert projection["parsed_action"] is None
    assert sink.transactions[0].attempted_failure_detail is not None


def test_off_base_path_reaches_provider_and_commits_collector_bound_detail(
    tmp_path: Path,
) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    audit, calls = _run_off_call(tmp_path, sink)

    assert len(calls) == 1
    receipt = audit.latest_completed_receipt
    assert receipt is not None
    assert receipt.provider_attempt_count == 1
    assert receipt.live_openai_calls == 0
    assert receipt.live_cost_usd_micros == 0
    assert receipt.live_cost_exact
    detail = sink.details[0]
    assert detail.pre_provider.status is ProductionRuntimeAuditPreProviderStatusV1.OFF
    attempt = detail.actor_provider_attempts[0]
    assert attempt.collector_request_locator["event_type"] == "model_request"
    assert attempt.collector_terminal_locator["event_type"] == "model_response"
    assert attempt.collector_request_locator["snapshot_blob"] is not None
    assert attempt.collector_terminal_locator["snapshot_blob"] is not None
    projection = production_runtime_audit_detail_projection(detail)
    encoded = json.dumps(projection, sort_keys=True)
    assert "Wait now." in encoded
    assert "PRIVATE_PROVIDER_OUTPUT" not in encoded
    assert "PRIVATE_PROVIDER_REASONING" not in encoded
    assert projection["terminal"]["parsed_action"]["action_type"] == WAIT


def test_external_sink_is_owner_only_and_transactionally_publishes(tmp_path: Path) -> None:
    output = tmp_path / "owner-only-production-audit"
    sink = ExternalProductionRuntimeAuditSinkV1(output)
    audit, _ = _run_off_call(tmp_path / "case", sink)
    receipt = audit.latest_completed_receipt
    assert receipt is not None
    destination = output / f"{receipt.logical_call_id}.production-runtime-audit.v1.json"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not tuple(output.glob("*.tmp"))
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["logical_call_id"] == receipt.logical_call_id


def test_inner_retry_has_exact_collector_and_provider_attempt_census(tmp_path: Path) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    agent, audit, calls = _off_agent(sink)
    outcomes: list[BaseException | _ActorResponse] = [
        RuntimeError("transient provider failure"),
        _ActorResponse("actor retry output"),
    ]

    def create(**kwargs: Any) -> _ActorResponse:
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    agent.openai_client.chat.completions.create = create
    run, context = _collector_context(tmp_path)
    action = JSONAction(action_type=WAIT)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=2,
            )
            assert prediction == "actor retry output"
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            agent.finalize_prompt_sentinel_action_execution(
                action=action,
                action_executed=False,
            )
    finally:
        run.close()
    assert len(calls) == 2
    assert audit.latest_completed_receipt is not None
    assert audit.latest_completed_receipt.provider_attempt_count == 2
    attempts = sink.details[0].actor_provider_attempts
    assert [item.attempt_index for item in attempts] == [1, 2]
    assert attempts[0].collector_terminal_locator["event_type"] == "model_attempt_failed"
    assert attempts[1].collector_terminal_locator["event_type"] == "model_response"


def test_strict_actor_dispatch_rechecks_deadline_and_returned_model_before_use() -> None:
    agent, _, _ = _off_agent(MemoryProductionRuntimeAuditSinkV1())
    sentinel = agent._prompt_sentinel
    logical_call = sentinel.logical_call(
        host_id=_Agent.sentinel_host_id,
        history_codec_id=_Agent.sentinel_history_codec_id,
        attributes={"r24_case_deadline_monotonic_ns": 1},
    )
    with bind_sentinel_logical_call(logical_call):
        assert agent._production_safe_logging_active()
        with pytest.raises(TimeoutError, match="deadline elapsed"):
            agent._production_dispatch_client()
        with pytest.raises(RuntimeError, match="returned model differs"):
            agent._require_production_response_model(
                SimpleNamespace(model="another-served-model"), "cpu-fake-actor"
            )


def test_elapsed_actor_deadline_has_no_dispatch_and_commits_typed_failure(tmp_path: Path) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    agent, audit, calls = _off_agent(sink)
    sentinel = agent._prompt_sentinel
    logical_call = sentinel.logical_call(
        host_id=_Agent.sentinel_host_id,
        history_codec_id=_Agent.sentinel_history_codec_id,
        attributes={"r24_case_deadline_monotonic_ns": 1},
    )
    run, context = _collector_context(tmp_path)
    try:
        with bind_audit_context(context), bind_sentinel_logical_call(logical_call):
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=3,
            )
            assert prediction is None
            failure = agent._finalize_prompt_sentinel_actor_failure(
                logical_call,
                failure_phase="ACTOR_DEADLINE",
                failure_code="ACTOR_CASE_DEADLINE_ELAPSED",
            )
    finally:
        run.close()
    assert calls == []
    assert failure.provider_attempt_count == 0
    assert failure.live_openai_calls == 0
    assert failure.failure_code == "ACTOR_CASE_DEADLINE_ELAPSED"
    assert audit.latest_failure_receipt == failure


def test_actor_provider_failure_publishes_terminal_negative_audit(tmp_path: Path) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    agent, audit, calls = _off_agent(sink)

    def fail(**kwargs: Any) -> _ActorResponse:
        calls.append(kwargs)
        raise RuntimeError("private provider failure")

    agent.openai_client.chat.completions.create = fail
    run, context = _collector_context(tmp_path)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=1,
            )
            assert prediction is None
            failure = agent._finalize_prompt_sentinel_actor_failure(
                logical_call,
                failure_phase="ACTOR_PROVIDER",
                failure_code="ACTOR_PROVIDER_FAILED",
            )
            assert failure is not None
    finally:
        run.close()

    assert len(calls) == 1
    assert audit.pending_count == 0
    receipt = audit.latest_failure_receipt
    assert receipt is not None
    assert receipt.failure_code == "ACTOR_PROVIDER_FAILED"
    assert receipt.provider_attempt_count == 1
    assert receipt.live_openai_calls == 0
    assert receipt.live_cost_exact
    assert len(sink.failure_details) == 1
    detail = sink.failure_details[0]
    assert type(detail) is dict
    assert detail["status"] == "FAILED"
    assert detail["actor_provider_attempts"][0]["status"] == "FAILED"


def test_missing_collector_request_locator_blocks_before_provider(tmp_path: Path) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    agent, audit, calls = _off_agent(sink)
    with agent._sentinel_logical_call_scope():
        with pytest.raises(ProductionRuntimeAuditError) as failure:
            agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=1,
            )
    assert failure.value.code == "COLLECTOR_LOCATOR_INVALID"
    assert calls == []
    assert audit.pending_count == 1


@pytest.mark.parametrize(
    ("host_id", "mode"),
    (
        ("mobileworld.qwen3vl.actor", SentinelMode.SHADOW),
        ("mobileworld.mai-ui.actor", SentinelMode.ACTIVE),
    ),
)
def test_qwen_and_mai_unsupported_shape_fallback_reaches_actor_provider(
    tmp_path: Path,
    host_id: str,
    mode: SentinelMode,
) -> None:
    policy = _unstarted_exact_live_policy()
    sink = MemoryProductionRuntimeAuditSinkV1()
    audit = ProductionRuntimeAuditV1(policy=policy, sink=sink)
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={host_id: SentinelHostConfig(mode=mode)},
        receipt_sink=MemorySentinelReceiptSink(),
        runtime_audit=audit,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: f"fallback-{mode.value.lower()}-call-1",
    )
    calls: list[dict[str, Any]] = []

    def respond(request: Request) -> Response:
        calls.append(json.loads(request.content))
        timeout = request.extensions.get("timeout")
        assert type(timeout) is dict
        assert all(float(value) <= 1.0 for value in timeout.values() if value is not None)
        return Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "index": 0,
                        "message": {"content": "actor fallback output", "role": "assistant"},
                    }
                ],
                "created": 1,
                "id": "actor-response-1",
                "model": "cpu-fake-actor",
                "object": "chat.completion",
                "usage": {
                    "completion_tokens": 7,
                    "prompt_tokens": 11,
                    "total_tokens": 18,
                },
            },
        )

    agent = _FallbackAgent(
        prompt_sentinel=sentinel,
        sentinel_host_id=host_id,
        sentinel_history_codec_id=None,
    )
    # Base's fallback to its class declaration is absent, so this is the
    # ordinary typed unsupported-family input before any live call occurs.
    agent._sentinel_history_codec_id = None
    agent.openai_client = OpenAI(
        base_url="http://127.0.0.1:1/v1",
        api_key="empty",
        max_retries=0,
        http_client=DefaultHttpxClient(
            transport=MockTransport(respond), trust_env=False, timeout=1.0
        ),
    )
    run, context = _collector_context(tmp_path)
    action = JSONAction(action_type=WAIT)
    try:
        logical_call = sentinel.logical_call(
            host_id=host_id,
            history_codec_id=None,
            attributes={"r24_case_deadline_monotonic_ns": monotonic_ns() + 1_000_000_000},
        )
        with bind_audit_context(context), bind_sentinel_logical_call(logical_call):
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=1,
            )
            assert prediction == "actor fallback output"
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            receipt = agent.finalize_prompt_sentinel_action_execution(
                action=action,
                action_executed=False,
            )
            assert receipt is not None
    finally:
        agent.openai_client.close()
        run.close()

    assert len(calls) == 1
    detail = sink.details[0]
    assert detail.pre_provider.status is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
    assert (
        receipt.pre_provider_outcome
        is production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL
    )
    assert receipt.fallback_reason is SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY
    assert detail.pre_provider.raw_request_sha256 == detail.pre_provider.final_request_sha256
    assert detail.pre_provider.live_openai_calls == 0
    assert detail.pre_provider.live_cost_exact
    projection = production_runtime_audit_detail_projection(detail)
    pre_provider = cast(dict[str, JsonValue], projection["pre_provider"])
    restricted = cast(dict[str, JsonValue], pre_provider["restricted_stage_projection"])
    assert restricted["r2_4_rubric_request_proofs"] == []


def test_production_no_history_untrusted_rubric_record_fails_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = cast(dict[str, Any], json.loads(QWEN_FIXTURE.read_text(encoding="utf-8")))
    request = cast(dict[str, JsonValue], deepcopy(fixture["application_request"]))
    messages = cast(list[JsonValue], request["messages"])
    content = cast(list[JsonValue], cast(dict[str, JsonValue], messages[1])["content"])
    text_block = cast(dict[str, JsonValue], content[0])
    text = cast(str, text_block["text"])
    text_block["text"] = text[: text.index("Step 1: ")] + "\n"

    policy = _unstarted_exact_live_policy()
    policy._attempt_sink = MemoryLiveAttemptReceiptSinkV1()
    calls: list[str] = []

    def prepare_no_history(
        self: OwnerAuthorizedLivePerCallPolicyV1,
        *,
        request: JsonValue,
        context: Any,
        execution_control: Any,
    ) -> object:
        del request, execution_control
        calls.append(context.logical_call_id)
        self._call_inputs[context.logical_call_id] = "registered-no-history"
        return object()

    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "prepare_no_history_with_control",
        prepare_no_history,
    )
    sink = MemoryProductionRuntimeAuditSinkV1()
    audit = ProductionRuntimeAuditV1(policy=policy, sink=sink)
    logical_call_id = "production-no-history-call-1"
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={
            "mobileworld.qwen3vl.actor": SentinelHostConfig(
                mode=SentinelMode.ACTIVE,
                policy_timeout_ms=1_000,
            )
        },
        receipt_sink=MemorySentinelReceiptSink(),
        runtime_audit=audit,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: logical_call_id,
    )
    logical_call = sentinel.logical_call(
        host_id="mobileworld.qwen3vl.actor",
        history_codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
    )

    first = logical_call.before_model_call(cast(JsonValue, request))
    second = logical_call.before_model_call(cast(JsonValue, request))

    assert type(first) is type(second)
    assert type(first) is not RuntimeVerticalSentinelResultV1
    assert first.receipt.fallback_reason is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
    assert first.raw_request == first.candidate_request == first.final_request == request
    assert second.raw_request == second.final_request == request
    assert calls == [logical_call_id]
    assert audit.pending_count == 1
    audit.cancel(logical_call_id)


def test_failed_live_attempt_root_does_not_require_completed_policy_binding() -> None:
    """Negative attempt evidence survives an Original fallback without inventing a call."""

    digest = "a" * 64
    attempt_hashes = ("b" * 64, "c" * 64)
    restricted: Any = {"kind": "FALLBACK_ORIGINAL"}
    restricted_hash = sha256(
        json.dumps(
            restricted,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    pre = production_audit_module.ProductionRuntimeAuditPreProviderV1(
        logical_call_id="fallback-negative-attempt-call-1",
        host_id="mobileworld.production-audit-test.actor",
        status=production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL,
        outcome=production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL,
        configured_mode=SentinelMode.ACTIVE,
        effective_mode=SentinelMode.OFF,
        fallback_reason=SentinelFallbackReason.POLICY_TIMEOUT,
        fallback_check="policy_timeout",
        raw_request_sha256=digest,
        extraction_sha256=None,
        history_ir_sha256=None,
        codec_overlay_sha256=None,
        vertical_output_sha256=None,
        coordinated_record_sha256=None,
        rubric_result_sha256=None,
        path_relevance_output_sha256=None,
        render_result_sha256=None,
        candidate_request_sha256=digest,
        exact_diff_sha256=digest,
        validator_result_sha256=digest,
        final_request_sha256=digest,
        live_call_binding_sha256=None,
        live_attempt_receipt_sha256s=attempt_hashes,
        live_attempt_receipt_root_sha256="d" * 64,
        case_execution_lease_sha256="e" * 64,
        preflight_report_sha256="f" * 64,
        factory_binding_sha256=None,
        execution_authority_sha256="1" * 64,
        source_transport_binding_sha256=None,
        pricing_binding_sha256="2" * 64,
        live_openai_calls=1,
        live_cost_usd_micros=7,
        live_cost_exact=True,
        restricted_stage_projection=restricted,
        restricted_stage_projection_sha256=restricted_hash,
        evidence_snapshot_ns=0,
        history_extract_ns=0,
        rubric_ns=1,
        policy_ns=1,
        render_ns=0,
        validator_ns=0,
        pre_provider_total_ns=1,
        _seal=production_audit_module._PRE_PROVIDER_SEAL,
    )
    assert pre.live_openai_calls == 1
    assert len(pre.live_attempt_receipt_sha256s) == 2

    receipt = production_audit_module.ProductionRuntimeAuditReceiptV1(
        detail_id="fallback-negative-attempt-detail-1",
        logical_call_id=pre.logical_call_id,
        raw_request_sha256=digest,
        final_request_sha256=digest,
        provider_request_sha256=digest,
        provider_response_sha256=digest,
        exact_diff_sha256=digest,
        pre_provider_sha256=digest,
        pre_provider_status=pre.status,
        pre_provider_outcome=pre.outcome,
        fallback_reason=pre.fallback_reason,
        fallback_check=pre.fallback_check,
        live_call_binding_sha256=None,
        live_attempt_receipt_root_sha256="d" * 64,
        actor_provider_attempt_root_sha256=digest,
        sentinel_receipt_sha256=digest,
        parser_input_sha256=digest,
        parser_result_sha256=digest,
        parsed_action_sha256=digest,
        action_executed=False,
        executed_action_sha256=None,
        provider_attempt_count=1,
        live_openai_calls=1,
        live_cost_usd_micros=7,
        live_cost_exact=True,
        total_ns=1,
        detail_sha256=digest,
        _seal=production_audit_module._RECEIPT_SEAL,
    )
    assert receipt.live_call_binding_sha256 is None
    assert receipt.live_attempt_receipt_root_sha256 == "d" * 64
