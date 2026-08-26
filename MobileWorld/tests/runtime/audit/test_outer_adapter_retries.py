from __future__ import annotations

import builtins
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations import general_e2e_agent as general_module
from mobile_world.agents.implementations import gui_owl_1_5 as gui_owl_module
from mobile_world.agents.implementations import memgui_agent as memgui_module
from mobile_world.agents.implementations import planner_executor as planner_module
from mobile_world.agents.implementations import qwen3vl as qwen_module
from mobile_world.agents.implementations import seed_agent as seed_module
from mobile_world.runtime.audit import context as context_module
from mobile_world.runtime.audit import ids as ids_module
from mobile_world.runtime.audit.context import (
    AuditContext,
    bind_audit_context,
    get_audit_context,
)
from mobile_world.runtime.audit.null_recorder import NULL_TASK_RECORDER

_EXPECTED_ATTEMPTS = {
    "qwen3vl": 4,
    "planner_executor": 3,
    "general_e2e": 3,
    "gui_owl_1_5": 5,
    "memgui": 3,
}

_BASE_RETRY_TIMES = {
    "qwen3vl": 3,
    "planner_executor": 1,
    "general_e2e": 1,
    "gui_owl_1_5": 3,
    "memgui": 3,
}

_EXPECTED_RETRY_PLANNED = {
    "qwen3vl": [False] * 4,
    "planner_executor": [True, True, False],
    "general_e2e": [True, True, False],
    "gui_owl_1_5": [False] * 5,
    "memgui": [False] * 3,
}

_COLLECTOR_FAULT_POINTS = (
    "context_import",
    "context_lookup",
    "retry_id_import",
    "retry_id",
    "derive",
    "bind_import",
    "bind_factory",
    "bind_enter",
    "bind_exit",
)


def _new_agent(case: str) -> Any:
    if case == "qwen3vl":
        agent = qwen_module.Qwen3VLAgentMCP.__new__(qwen_module.Qwen3VLAgentMCP)
        BaseAgent.__init__(agent)
        agent.model_name = "fake-model"
        agent.runtime_conf = {}
        agent.instruction = "wait"
        agent.tools = []
        agent.actions = []
        agent.thoughts = []
        agent.conclusions = []
        agent.history_images = []
        agent.history_responses = []
        return agent

    if case == "planner_executor":
        agent = planner_module.PlannerExecutorAgentMCP.__new__(
            planner_module.PlannerExecutorAgentMCP
        )
        BaseAgent.__init__(agent)
        agent.model_name = "fake-model"
        agent.llm_base_url = "https://model.invalid/v1"
        agent.api_key = "empty"
        agent.runtime_conf = {}
        agent.instruction = "wait"
        agent.tools = []
        agent.executor = None
        agent.history_n_images = 3
        agent.history_images = []
        agent.history_responses = []
        agent.actions = []
        agent.plans = []
        return agent

    if case == "general_e2e":
        agent = general_module.GeneralE2EAgentMCP.__new__(general_module.GeneralE2EAgentMCP)
        BaseAgent.__init__(agent)
        agent.model_name = "fake-model"
        agent.llm_base_url = "https://model.invalid/v1"
        agent.api_key = "empty"
        agent.runtime_conf = {}
        agent.instruction = "wait"
        agent.tools = []
        agent.scale_factor = 1000
        agent._use_adaptive_resize = False
        agent.history_n_images = 3
        agent.history_images = []
        agent.history_responses = []
        agent.actions = []
        return agent

    if case == "gui_owl_1_5":
        agent = gui_owl_module.GUIOWL15AgentMCP.__new__(gui_owl_module.GUIOWL15AgentMCP)
        BaseAgent.__init__(agent)
        agent.model_name = "fake-model"
        agent.runtime_conf = {}
        agent.instruction = "wait"
        agent.tools = []
        agent.temperature = 0.0
        agent.top_p = 1.0
        agent.max_tokens = 128
        agent.history_n = 1
        agent.actions = []
        agent.thoughts = []
        agent.conclusions = []
        agent.history_images = []
        agent.history_responses = []
        agent.history_user_content = []
        return agent

    if case == "memgui":
        agent = memgui_module.MemGUIAgent.__new__(memgui_module.MemGUIAgent)
        BaseAgent.__init__(agent)
        agent.model_name = "fake-model"
        agent.runtime_conf = {}
        agent.instruction = "wait"
        agent.current_step = 0
        agent.state_summaries = []
        agent.latest_interaction = None
        agent.memory_state = {}
        agent.history_responses = []
        agent.thoughts = []
        agent.ui_observations = []
        agent.action_intents = []
        agent.folding_stats = {
            "step_level_distillations": 0,
            "span_level_abstractions": 0,
            "total_steps_folded": 0,
        }
        return agent

    raise AssertionError(f"unknown case: {case}")


def _patch_parser(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    success_attempt: int,
) -> None:
    parse_attempt = 0

    def should_succeed() -> None:
        nonlocal parse_attempt
        parse_attempt += 1
        if parse_attempt < success_attempt:
            raise ValueError(f"malformed response {parse_attempt}")

    if case == "qwen3vl":

        def parse_qwen(_response: Any) -> dict[str, Any]:
            should_succeed()
            return {
                "thinking": "done",
                "conclusion": "wait",
                "action_json": {"action": "wait"},
                "action_name": "mobile_use",
            }

        monkeypatch.setattr(qwen_module, "parse_action_to_structure_output", parse_qwen)
        return

    if case == "planner_executor":

        def parse_planner(_response: Any) -> tuple[str, str]:
            should_succeed()
            return "done", '{"action_type":"wait"}'

        monkeypatch.setattr(planner_module, "parse_action", parse_planner)
        return

    if case == "general_e2e":

        def parse_general(_response: Any) -> tuple[str, str]:
            should_succeed()
            return "done", '{"action_type":"wait"}'

        monkeypatch.setattr(general_module, "parse_action", parse_general)
        return

    if case == "gui_owl_1_5":

        def parse_gui_owl(_response: Any) -> dict[str, Any]:
            should_succeed()
            return {
                "thinking": "done",
                "conclusion": "wait",
                "action_json": {"action": "wait"},
                "action_name": "mobile_use",
            }

        monkeypatch.setattr(
            gui_owl_module,
            "parse_action_to_structure_output",
            parse_gui_owl,
        )
        return

    if case == "memgui":

        def parse_memgui(_response: Any, **_kwargs: Any) -> dict[str, Any]:
            should_succeed()
            return {
                "thinking": "done",
                "folding_directive": None,
                "action_json": {"action": "wait"},
                "action_name": "mobile_use",
                "ui_observation": "screen",
                "action_intent": "wait",
                "memory_args": None,
            }

        monkeypatch.setattr(memgui_module, "parse_memgui_response", parse_memgui)
        return

    raise AssertionError(f"unknown case: {case}")


def _invoke(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    success_attempt: int,
) -> tuple[Any, list[AuditContext | None], list[int], list[int]]:
    agent = _new_agent(case)
    _patch_parser(case, monkeypatch, success_attempt=success_attempt)
    observed_contexts: list[AuditContext | None] = []
    message_ids: list[int] = []
    retry_times: list[int] = []

    def fake_completion(**kwargs: Any) -> str:
        observed_contexts.append(get_audit_context())
        message_ids.append(id(kwargs["messages"]))
        retry_times.append(kwargs["retry_times"])
        return f"response-{len(observed_contexts)}"

    monkeypatch.setattr(agent, "openai_chat_completions_create", fake_completion)
    result = agent.predict({"screenshot": Image.new("RGB", (8, 6))})
    return result, observed_contexts, message_ids, retry_times


def _install_collector_fault(
    fault_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(f"collector {fault_point} failed")

    if fault_point in {"context_import", "retry_id_import", "bind_import"}:
        original_import = builtins.__import__
        context_imports = 0

        def faulting_import(
            name: str,
            globals: dict[str, Any] | None = None,
            locals: dict[str, Any] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> Any:
            nonlocal context_imports
            if name == "mobile_world.runtime.audit.context":
                context_imports += 1
                if fault_point == "context_import" or (
                    fault_point == "bind_import" and context_imports >= 2
                ):
                    raise ImportError(f"collector {fault_point} failed")
            if fault_point == "retry_id_import" and name == "mobile_world.runtime.audit.ids":
                raise ImportError("collector retry_id_import failed")
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", faulting_import)
        return
    if fault_point == "context_lookup":
        monkeypatch.setattr(context_module, "get_audit_context", fail)
        return
    if fault_point == "retry_id":
        monkeypatch.setattr(ids_module, "new_ulid", fail)
        return
    if fault_point == "derive":
        monkeypatch.setattr(AuditContext, "derive", fail)
        return
    if fault_point == "bind_factory":
        monkeypatch.setattr(context_module, "bind_audit_context", fail)
        return
    if fault_point == "bind_enter":

        class BrokenEnter:
            def __enter__(self) -> Any:
                raise OSError("collector bind enter failed")

            def __exit__(self, *_args: Any) -> bool:
                return False

        monkeypatch.setattr(
            context_module,
            "bind_audit_context",
            lambda _context: BrokenEnter(),
        )
        return
    if fault_point == "bind_exit":
        original_bind = context_module.bind_audit_context

        class BrokenExit:
            def __init__(self, context: AuditContext) -> None:
                self._inner = original_bind(context)

            def __enter__(self) -> Any:
                return self._inner.__enter__()

            def __exit__(self, *args: Any) -> bool:
                self._inner.__exit__(*args)
                raise OSError("collector bind exit failed")

        monkeypatch.setattr(context_module, "bind_audit_context", BrokenExit)
        return
    raise AssertionError(f"unknown collector fault: {fault_point}")


def _invoke_seed_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, list[AuditContext | None], list[int], list[BaseException]]:
    agent = seed_module.SeedAgent.__new__(seed_module.SeedAgent)
    BaseAgent.__init__(agent)
    messages = [{"role": "user", "content": "same seed request"}]
    observed_contexts: list[AuditContext | None] = []
    message_ids: list[int] = []
    provider_errors: list[BaseException] = []

    def fake_inference(actual_messages: list[dict]) -> str:
        observed_contexts.append(get_audit_context())
        message_ids.append(id(actual_messages))
        if len(observed_contexts) == 1:
            error = TimeoutError("original seed provider timeout")
            provider_errors.append(error)
            raise error
        return "seed-result"

    monkeypatch.setattr(agent, "_inference_with_thinking", fake_inference)
    result = agent._inference_with_retries(messages)
    return result, observed_contexts, message_ids, provider_errors


def _invoke_general_provider_retry(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: BaseException,
) -> tuple[Any, list[dict[str, Any]], list[float]]:
    agent = _new_agent("general_e2e")
    calls: list[dict[str, Any]] = []
    sleeps: list[float] = []

    monkeypatch.setattr(
        general_module,
        "parse_action",
        lambda _response: ("done", '{"action_type":"wait"}'),
    )
    monkeypatch.setattr(general_module.time, "sleep", sleeps.append)

    def fake_completion(**kwargs: Any) -> str:
        calls.append(kwargs)
        if len(calls) == 1:
            raise provider_error
        return "provider-retry-result"

    monkeypatch.setattr(agent, "openai_chat_completions_create", fake_completion)
    result = agent.predict({"screenshot": Image.new("RGB", (8, 6))})
    return result, calls, sleeps


@pytest.mark.parametrize("case", tuple(_EXPECTED_ATTEMPTS))
def test_outer_adapter_retries_share_enabled_audit_correlation(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_attempts = _EXPECTED_ATTEMPTS[case]
    parent = AuditContext(
        run_id="run",
        recorder=SimpleNamespace(enabled=True),
        model_call_id="parent-model-call",
    )

    with bind_audit_context(parent):
        result, contexts, message_ids, retry_times = _invoke(
            case,
            monkeypatch,
            success_attempt=expected_attempts,
        )
        assert get_audit_context() is parent

    assert result[0] == f"response-{expected_attempts}"
    assert len(contexts) == expected_attempts
    assert all(context is not None and context is not parent for context in contexts)
    assert [context.adapter_attempt_index for context in contexts if context] == list(
        range(1, expected_attempts + 1)
    )
    assert [
        context.adapter_retry_planned for context in contexts if context
    ] == _EXPECTED_RETRY_PLANNED[case]
    retry_group_ids = {context.retry_group_id for context in contexts if context}
    assert len(retry_group_ids) == 1
    assert None not in retry_group_ids
    assert all(context.model_call_id is None for context in contexts if context)
    assert len(set(message_ids)) == 1
    assert retry_times == [_BASE_RETRY_TIMES[case]] * expected_attempts


@pytest.mark.parametrize("case", tuple(_EXPECTED_ATTEMPTS))
def test_outer_adapter_retries_feature_off_allocate_no_id_or_child_context(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_context = AuditContext(run_id="disabled", recorder=NULL_TASK_RECORDER)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("feature-off outer retry entered audit correlation")

    with bind_audit_context(original_context):
        monkeypatch.setattr(ids_module, "new_ulid", forbidden)
        monkeypatch.setattr(context_module, "bind_audit_context", forbidden)
        monkeypatch.setattr(AuditContext, "derive", forbidden)
        result, contexts, message_ids, retry_times = _invoke(
            case,
            monkeypatch,
            success_attempt=2,
        )
        assert get_audit_context() is original_context

    assert result[0] == "response-2"
    assert contexts == [original_context, original_context]
    assert len(set(message_ids)) == 1
    assert retry_times == [_BASE_RETRY_TIMES[case], _BASE_RETRY_TIMES[case]]


@pytest.mark.parametrize("case", tuple(_EXPECTED_ATTEMPTS))
@pytest.mark.parametrize("fault_point", _COLLECTOR_FAULT_POINTS)
def test_outer_adapter_collector_faults_match_provider_behavior(
    case: str,
    fault_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = AuditContext(
        run_id="run",
        recorder=SimpleNamespace(enabled=True),
        model_call_id="parent-model-call",
    )
    sleeps: list[float] = []
    monkeypatch.setattr(general_module.time, "sleep", sleeps.append)

    with bind_audit_context(parent):
        baseline_result, baseline_contexts, baseline_message_ids, baseline_retry_times = _invoke(
            case,
            monkeypatch,
            success_attempt=2,
        )
        _install_collector_fault(fault_point, monkeypatch)
        faulted_result, faulted_contexts, faulted_message_ids, faulted_retry_times = _invoke(
            case,
            monkeypatch,
            success_attempt=2,
        )
        assert get_audit_context() is parent

    assert faulted_result == baseline_result
    assert len(faulted_contexts) == len(baseline_contexts) == 2
    assert len(set(baseline_message_ids)) == len(set(faulted_message_ids)) == 1
    assert faulted_retry_times == baseline_retry_times == [_BASE_RETRY_TIMES[case]] * 2
    assert sleeps == []


@pytest.mark.parametrize("fault_point", _COLLECTOR_FAULT_POINTS)
def test_seed_outer_retry_collector_faults_preserve_calls_identity_and_result(
    fault_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = AuditContext(
        run_id="run",
        recorder=SimpleNamespace(enabled=True),
        model_call_id="parent-model-call",
    )
    with bind_audit_context(parent):
        _install_collector_fault(fault_point, monkeypatch)
        result, contexts, message_ids, provider_errors = _invoke_seed_retry(monkeypatch)
        assert get_audit_context() is parent

    assert result == "seed-result"
    assert len(contexts) == 2
    assert len(set(message_ids)) == 1
    assert len(provider_errors) == 1


@pytest.mark.parametrize("fault_point", _COLLECTOR_FAULT_POINTS)
def test_collector_fault_preserves_provider_retry_arguments_and_sleep(
    fault_point: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = AuditContext(
        run_id="run",
        recorder=SimpleNamespace(enabled=True),
        model_call_id="parent-model-call",
    )
    baseline_error = TimeoutError("exact baseline provider timeout")
    faulted_error = TimeoutError("exact faulted provider timeout")

    with bind_audit_context(parent):
        baseline_result, baseline_calls, baseline_sleeps = _invoke_general_provider_retry(
            monkeypatch,
            baseline_error,
        )
        _install_collector_fault(fault_point, monkeypatch)
        faulted_result, faulted_calls, faulted_sleeps = _invoke_general_provider_retry(
            monkeypatch,
            faulted_error,
        )
        assert get_audit_context() is parent

    assert faulted_result == baseline_result
    assert len(faulted_calls) == len(baseline_calls) == 2
    assert len({id(call["messages"]) for call in baseline_calls}) == 1
    assert len({id(call["messages"]) for call in faulted_calls}) == 1
    assert [call["retry_times"] for call in faulted_calls] == [1, 1]
    assert [call["retry_times"] for call in baseline_calls] == [1, 1]
    assert faulted_sleeps == baseline_sleeps == [2]


def test_bind_exit_fault_cannot_replace_or_repeat_original_provider_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = AuditContext(
        run_id="run",
        recorder=SimpleNamespace(enabled=True),
        retry_group_id="existing-retry-group",
    )
    agent = _new_agent("general_e2e")
    provider_error = RuntimeError("exact original provider exception")
    provider_calls: list[object] = []

    with bind_audit_context(parent):
        retry_group = agent._begin_outer_model_audit_retry_group()
        assert retry_group is not None
        _install_collector_fault("bind_exit", monkeypatch)
        with pytest.raises(RuntimeError) as raised:
            with agent._outer_model_audit_attempt_scope(
                retry_group,
                adapter_attempt_index=1,
                adapter_retry_planned=False,
            ):
                provider_calls.append(object())
                raise provider_error
        assert get_audit_context() is parent

    assert raised.value is provider_error
    assert len(provider_calls) == 1
