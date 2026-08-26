from concurrent.futures import ThreadPoolExecutor

import pytest

from mobile_world.runtime.audit.context import (
    AuditContext,
    ModelCallTrace,
    bind_audit_context,
    bind_audit_scope,
    get_audit_context,
    get_current_recorder,
    require_audit_context,
)


def test_binding_is_nested_and_restored() -> None:
    recorder = object()
    parent = AuditContext(run_id="run", recorder=recorder)

    assert get_audit_context() is None
    assert get_current_recorder() is None

    with bind_audit_context(parent):
        assert get_audit_context() is parent
        assert get_current_recorder() is recorder

        with bind_audit_scope(task_run_id="task", step_id="step") as child:
            assert child is get_audit_context()
            assert child.run_id == "run"
            assert child.recorder is recorder
            assert child.task_run_id == "task"
            assert child.step_id == "step"

        assert get_audit_context() is parent

    assert get_audit_context() is None


def test_binding_resets_in_finally_after_exception() -> None:
    context = AuditContext(run_id="run", recorder=object())

    with pytest.raises(RuntimeError, match="boom"):
        with bind_audit_context(context):
            raise RuntimeError("boom")

    assert get_audit_context() is None
    with pytest.raises(LookupError, match="no audit context"):
        require_audit_context()


def test_context_does_not_leak_into_a_reused_worker_thread() -> None:
    context = AuditContext(run_id="run", recorder=object(), task_run_id="task")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with bind_audit_context(context):
            assert executor.submit(get_audit_context).result() is None
        assert executor.submit(get_audit_context).result() is None


def test_context_drops_only_exact_local_api_key_placeholders() -> None:
    context = AuditContext(
        run_id="run",
        recorder=object(),
        known_secrets=("EMPTY", "real-secret", "EMPTY-extra"),
    )

    assert context.known_secrets == ("real-secret", "EMPTY-extra")


def test_model_call_trace_is_per_step_ordered_and_deduplicated() -> None:
    trace = ModelCallTrace()
    context = AuditContext(run_id="run", recorder=object(), model_call_trace=trace)

    context.record_model_call("actor-call")
    context.record_model_call("grounder-call")
    context.record_model_call("actor-call")
    trace.record_terminal("actor-call", "actor-terminal")
    trace.record_terminal("grounder-call", "grounder-terminal")

    assert context.source_model_call_ids() == ("actor-call", "grounder-call")
    assert context.latest_model_terminal_event_id() == "grounder-terminal"
    assert (
        context.derive(step_id="next", model_call_trace=ModelCallTrace()).source_model_call_ids()
        == ()
    )


def test_model_call_trace_is_safe_for_concurrent_hooks() -> None:
    trace = ModelCallTrace()
    context = AuditContext(run_id="run", recorder=object(), model_call_trace=trace)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(context.record_model_call, [f"call-{index % 10}" for index in range(100)])
        )

    assert set(context.source_model_call_ids()) == {f"call-{index}" for index in range(10)}
    assert len(context.source_model_call_ids()) == 10
