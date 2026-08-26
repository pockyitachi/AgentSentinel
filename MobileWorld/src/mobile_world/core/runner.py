from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from queue import Queue
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv
from joblib import Parallel, delayed
from loguru import logger

from mobile_world.agents.base import BaseAgent, MCPAgent
from mobile_world.agents.registry import create_agent
from mobile_world.runtime.client import (
    AndroidEnvClient,
    AndroidMCPEnvClient,
    scan_finished_tasks,
)
from mobile_world.runtime.utils.docker import (
    discover_backends,
)
from mobile_world.runtime.utils.models import ANSWER, ENV_FAIL, FINISHED, UNKNOWN
from mobile_world.runtime.utils.trajectory_logger import TrajLogger

if TYPE_CHECKING:
    from mobile_world.runtime.audit.runner_capture import (
        RunnerTaskCapture,
        RunnerTaskMetadata,
    )

load_dotenv()


def _safe_collector_call(callback: Any, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Call collector-owned code without allowing it onto the business path."""

    try:
        if not callable(callback):
            return default
        return callback(*args, **kwargs)
    except Exception:
        return default


def _safe_collector_method(
    target: Any,
    method_name: str,
    *args: Any,
    default: Any = None,
    missing: tuple[str, ...] = (),
    **kwargs: Any,
) -> Any:
    """Invoke one collector hook and best-effort mark a failed hook incomplete."""

    try:
        method = getattr(target, method_name)
        return method(*args, **kwargs)
    except Exception:
        if method_name != "mark_incomplete" and missing:
            try:
                marker = getattr(target, "mark_incomplete", None)
                if callable(marker):
                    marker(*missing)
            except Exception:
                pass
        return default


def _safe_collector_attr(target: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(target, name)
    except Exception:
        return default


@contextmanager
def _passive_collector_context(manager: Any) -> Iterator[Any]:
    """Enter/reset a collector context without masking live exceptions."""

    try:
        value = manager.__enter__()
    except Exception:
        yield None
        return
    try:
        yield value
    except BaseException as error:
        try:
            manager.__exit__(type(error), error, error.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass


def _close_decision_after_runner_error(
    audit_capture: RunnerTaskCapture,
    decision: Any,
) -> None:
    """Best-effort terminal for a decision whose local runner processing failed."""

    if decision is None:
        try:
            audit_capture.mark_incomplete("transition_not_executed.runner_exception")
        except Exception:
            pass
        return
    try:
        event = audit_capture.transition_not_executed(
            reason="runner_exception",
            decision=decision,
        )
    except Exception:
        event = None
    if event is None:
        try:
            audit_capture.mark_incomplete("transition_not_executed.runner_exception")
        except Exception:
            pass


def _record_pre_goal_task_failure(
    audit_capture: RunnerTaskCapture,
    audit_metadata: RunnerTaskMetadata,
    *,
    task_name: str,
    termination_source: str,
    error: BaseException,
) -> None:
    """Close a bound attempt that failed before goal retrieval was attempted."""

    _safe_collector_method(
        audit_capture,
        "start_task",
        task_name=task_name,
        task_goal=None,
        task_goal_status="retrieval_failed",
        task_index=_safe_collector_attr(audit_metadata, "task_index", 1),
        suite_family=_safe_collector_attr(audit_metadata, "suite_family", "mobile_world"),
        agent=_safe_collector_attr(audit_metadata, "agent", {}),
        environment=_safe_collector_attr(audit_metadata, "environment", {}),
        whole_task_attempt_index=_safe_collector_attr(
            audit_metadata,
            "whole_task_attempt_index",
            1,
        ),
        missing=("task_started",),
    )
    _safe_collector_method(
        audit_capture,
        "end_task",
        runtime_status="crashed",
        termination_source=termination_source,
        final_step_index=0,
        termination_exception=error,
        missing=("task_ended",),
    )


def _execute_single_task(
    env: AndroidEnvClient,
    agent: BaseAgent,
    task_name: str,
    max_step: int,
    traj_logger: TrajLogger,
    enable_mcp: bool = False,
    *,
    audit_capture: RunnerTaskCapture | None = None,
    audit_metadata: RunnerTaskMetadata | None = None,
    audit_runtime_status_callback: Callable[[str], None] | None = None,
) -> tuple[int, float]:
    """Execute a single task and return the number of steps and score.

    Returns:
        tuple[int, float]: (number of steps, score)
    """

    audit_enabled = _safe_collector_call(
        bool,
        _safe_collector_attr(audit_capture, "enabled", False),
        default=False,
    )
    if audit_capture is not None and audit_enabled:
        if audit_metadata is None:
            _safe_collector_method(
                audit_capture,
                "mark_incomplete",
                "runner_task_metadata",
            )
        else:
            return _execute_single_task_with_audit(
                env,
                agent,
                task_name,
                max_step,
                traj_logger,
                enable_mcp=enable_mcp,
                audit_capture=audit_capture,
                audit_metadata=audit_metadata,
                audit_runtime_status_callback=audit_runtime_status_callback,
            )

    logger.debug(f"max_step: {max_step}")

    if enable_mcp and not isinstance(agent, MCPAgent):
        logger.error(
            "MCP is enabled but agent type is not a MCP agent. Please use a MCP agent type."
        )

    if enable_mcp:
        traj_logger.log_tools(env.tools)
    task_goal = env.get_task_goal(task_type=task_name)

    logger.debug(f"task_goal: {task_goal}")

    step = 0
    obs = env.initialize_task(task_name=task_name)
    agent.initialize(task_goal)

    while True:
        step += 1

        logger.debug(f"Screenshot captured in step {step}")

        prediction, action = agent.predict(
            {
                "screenshot": obs.screenshot,
                "tool_call": obs.tool_call,
                "ask_user_response": obs.ask_user_response,
            }
        )  # for backward compatibility
        traj_logger.log_traj(
            task_name,
            task_goal,
            step,
            prediction,
            action.model_dump(exclude_none=True),
            obs,
            agent.get_total_token_usage(),
        )
        if prediction is None:
            logger.warning(f"Agent prediction failed in step {step}")
            break

        terminate = False
        logger.debug(f"current step {step}")

        if action.action_type in [ENV_FAIL, FINISHED, UNKNOWN]:
            logger.debug(f"task terminated in step {step} with action {action.action_type}")
            terminate = True
        elif action.action_type in [ANSWER]:
            logger.debug(f"answer triggered, execution action {action}")
            obs = env.execute_action(action)
            terminate = True
        else:
            logger.debug(f"execution action {action}")
            obs = env.execute_action(action)
        if terminate:
            break

        if step >= max_step:
            logger.debug("task steps reach max step, terminate")
            break

    score, reason = env.get_task_score(task_type=task_name)
    logger.debug(f"task_score: {score}, reason: {reason}")
    traj_logger.log_score(score=score, reason=reason)

    res = env.tear_down_task(task_type=task_name)
    agent.done()
    logger.debug(f"tear_down_task response: {res}")

    return step, score


def _execute_single_task_with_audit(
    env: AndroidEnvClient,
    agent: BaseAgent,
    task_name: str,
    max_step: int,
    traj_logger: TrajLogger,
    *,
    enable_mcp: bool,
    audit_capture: RunnerTaskCapture,
    audit_metadata: RunnerTaskMetadata,
    audit_runtime_status_callback: Callable[[str], None] | None,
) -> tuple[int, float]:
    """Run the existing control flow with passive, enabled audit boundaries."""

    from mobile_world.runtime.audit.context import (
        AuditContext,
        ModelCallTrace,
        bind_audit_context,
    )
    from mobile_world.runtime.audit.execution_io import (
        ExecutionEvidenceTrace,
        bind_execution_evidence_trace,
    )

    logger.debug(f"max_step: {max_step}")

    if enable_mcp and not isinstance(agent, MCPAgent):
        logger.error(
            "MCP is enabled but agent type is not a MCP agent. Please use a MCP agent type."
        )

    task_context = _safe_collector_call(
        AuditContext,
        run_id=_safe_collector_attr(audit_metadata, "run_id"),
        recorder=_safe_collector_attr(audit_capture, "task_recorder"),
        task_run_id=_safe_collector_attr(audit_metadata, "task_run_id"),
        store_stream_chunks=_safe_collector_attr(
            audit_metadata,
            "store_stream_chunks",
            True,
        ),
        known_secrets=_safe_collector_attr(audit_capture, "configured_secrets", ()),
    )
    if task_context is None:
        _safe_collector_method(
            audit_capture,
            "mark_incomplete",
            "audit_context",
        )

    if enable_mcp:
        try:
            traj_logger.log_tools(env.tools)
        except Exception as error:
            try:
                _record_pre_goal_task_failure(
                    audit_capture,
                    audit_metadata,
                    task_name=task_name,
                    termination_source="trajectory_tool_logging",
                    error=error,
                )
            finally:
                _safe_collector_call(audit_runtime_status_callback, "crashed")
            raise
    step = 0
    score: float | None = None
    reason: str | None = None
    latest_token_usage: dict[str, int] = {}
    teardown_attempted = False
    teardown_result: Any = None
    teardown_exception: BaseException | None = None
    evaluation_exception: BaseException | None = None
    termination_exception: BaseException | None = None
    runtime_status = "crashed"
    termination_source = "uncaught_exception"
    active_phase = "task_goal_retrieval"
    task_started = False

    execution_trace = None
    audit_stack = ExitStack()
    task_binding = _safe_collector_call(bind_audit_context, task_context)
    with _passive_collector_context(task_binding):
        try:
            try:
                task_goal = env.get_task_goal(task_type=task_name)
            except Exception:
                task_started = True
                _safe_collector_method(
                    audit_capture,
                    "start_task",
                    task_name=task_name,
                    task_goal=None,
                    task_goal_status="retrieval_failed",
                    task_index=_safe_collector_attr(audit_metadata, "task_index", 1),
                    suite_family=_safe_collector_attr(
                        audit_metadata,
                        "suite_family",
                        "mobile_world",
                    ),
                    agent=_safe_collector_attr(audit_metadata, "agent", {}),
                    environment=_safe_collector_attr(audit_metadata, "environment", {}),
                    whole_task_attempt_index=_safe_collector_attr(
                        audit_metadata,
                        "whole_task_attempt_index",
                        1,
                    ),
                    missing=("task_started",),
                )
                raise

            logger.debug(f"task_goal: {task_goal}")
            task_started = True
            _safe_collector_method(
                audit_capture,
                "start_task",
                task_name=task_name,
                task_goal=task_goal,
                task_goal_status="resolved",
                task_index=_safe_collector_attr(audit_metadata, "task_index", 1),
                suite_family=_safe_collector_attr(
                    audit_metadata,
                    "suite_family",
                    "mobile_world",
                ),
                agent=_safe_collector_attr(audit_metadata, "agent", {}),
                environment=_safe_collector_attr(audit_metadata, "environment", {}),
                whole_task_attempt_index=_safe_collector_attr(
                    audit_metadata,
                    "whole_task_attempt_index",
                    1,
                ),
                missing=("task_started",),
            )

            # task_started must be the first task-stream event.  Trace setup
            # can itself emit a fail-open collector_error, so defer it until
            # after the task-start boundary has been attempted.
            execution_trace = _safe_collector_call(ExecutionEvidenceTrace.from_context)
            trace_binding = _safe_collector_call(
                bind_execution_evidence_trace,
                execution_trace,
            )
            task_context = _safe_collector_method(
                task_context,
                "derive",
                execution_evidence_trace=execution_trace,
                default=task_context,
            )
            _safe_collector_call(audit_stack.enter_context, trace_binding)

            active_phase = "environment_initialization"
            obs = env.initialize_task(task_name=task_name)
            active_phase = "agent_initialization"
            agent.initialize(task_goal)

            while True:
                step += 1
                logger.debug(f"Screenshot captured in step {step}")
                agent_observation = {
                    "screenshot": obs.screenshot,
                    "tool_call": obs.tool_call,
                    "ask_user_response": obs.ask_user_response,
                }
                source_bytes = (
                    _safe_collector_method(
                        execution_trace,
                        "source_screenshot_bytes",
                        obs.screenshot,
                        missing=("observation.screenshot.source_blob",),
                    )
                    if execution_trace is not None
                    else None
                )
                step_reference = _safe_collector_method(
                    audit_capture,
                    "start_step",
                    step_index=step,
                    observation=agent_observation,
                    source_screenshot_bytes=source_bytes,
                    missing=("step_started.observation",),
                )
                model_call_trace = _safe_collector_call(ModelCallTrace)
                if step_reference is not None:
                    step_context = _safe_collector_method(
                        task_context,
                        "derive",
                        step_id=step_reference.step_id,
                        decision_id=step_reference.decision_id,
                        model_call_trace=model_call_trace,
                        parent_event_id=step_reference.step_started_event_id,
                        default=task_context,
                    )
                else:
                    step_context = _safe_collector_method(
                        task_context,
                        "derive",
                        model_call_trace=model_call_trace,
                        default=task_context,
                    )

                step_binding = _safe_collector_call(bind_audit_context, step_context)
                with _passive_collector_context(step_binding):
                    active_phase = "agent_prediction"
                    try:
                        prediction, action = agent.predict(agent_observation)
                    except Exception as error:
                        active_phase = "prediction_exception"
                        decision = (
                            _safe_collector_method(
                                audit_capture,
                                "record_decision",
                                prediction=None,
                                action=None,
                                parse_exception=error,
                                model_call_trace=model_call_trace,
                                missing=("agent_decision",),
                            )
                            if step_reference is not None
                            else None
                        )
                        if decision is not None:
                            _safe_collector_method(
                                audit_capture,
                                "transition_not_executed",
                                reason="prediction_exception",
                                decision=decision,
                                missing=("transition_not_executed",),
                            )
                        raise

                    decision = (
                        _safe_collector_method(
                            audit_capture,
                            "record_decision",
                            prediction=prediction,
                            action=action,
                            model_call_trace=model_call_trace,
                            missing=("agent_decision",),
                        )
                        if step_reference is not None
                        else None
                    )
                    active_phase = "trajectory_logging"
                    try:
                        action_log = action.model_dump(exclude_none=True)
                        latest_token_usage = agent.get_total_token_usage()
                        traj_logger.log_traj(
                            task_name,
                            task_goal,
                            step,
                            prediction,
                            action_log,
                            obs,
                            latest_token_usage,
                        )
                    except Exception:
                        _close_decision_after_runner_error(audit_capture, decision)
                        raise
                    if prediction is None:
                        try:
                            logger.warning(f"Agent prediction failed in step {step}")
                        except Exception:
                            _close_decision_after_runner_error(audit_capture, decision)
                            raise
                        if decision is not None:
                            _safe_collector_method(
                                audit_capture,
                                "transition_not_executed",
                                reason="prediction_none",
                                decision=decision,
                                missing=("transition_not_executed",),
                            )
                        runtime_status = "aborted"
                        termination_source = "prediction_none"
                        break

                    terminate = False

                    try:
                        logger.debug(f"current step {step}")
                        is_terminal_action = action.action_type in [ENV_FAIL, FINISHED, UNKNOWN]
                        if is_terminal_action:
                            logger.debug(
                                f"task terminated in step {step} with action {action.action_type}"
                            )
                    except Exception:
                        _close_decision_after_runner_error(audit_capture, decision)
                        raise

                    if is_terminal_action:
                        if decision is not None:
                            _safe_collector_method(
                                audit_capture,
                                "transition_not_executed",
                                reason="terminal_action",
                                decision=decision,
                                missing=("transition_not_executed",),
                            )
                        terminate = True
                        termination_source = "agent_terminal_action"
                    else:
                        execution = (
                            _safe_collector_method(
                                audit_capture,
                                "execution_started",
                                decision=decision,
                                missing=("action_execution_started",),
                            )
                            if decision is not None
                            else None
                        )
                        if execution is None:
                            _safe_collector_method(
                                audit_capture,
                                "mark_incomplete",
                                "action_execution_started",
                                "transition.execution_result",
                            )
                        if action.action_type in [ANSWER]:
                            logger.debug(f"answer triggered, execution action {action}")
                            terminate = True
                            termination_source = "answer_action"
                        else:
                            logger.debug(f"execution action {action}")

                        fallback_started_ns = time.monotonic_ns()
                        if execution_trace is not None and execution is not None:
                            _safe_collector_method(
                                execution_trace,
                                "begin_execution",
                                execution_kind=execution.execution_kind,
                                missing=("transition.execution_result",),
                            )
                        active_phase = "action_execution"
                        try:
                            post_observation = env.execute_action(action)
                        except Exception as error:
                            active_phase = "action_execution_exception"
                            evidence = (
                                _safe_collector_method(
                                    execution_trace,
                                    "fail_execution",
                                    error,
                                    missing=("transition.execution_result",),
                                )
                                if execution_trace is not None and execution is not None
                                else None
                            )
                            if execution is not None:
                                _safe_collector_method(
                                    audit_capture,
                                    "transition_failed",
                                    exception=error,
                                    execution=execution,
                                    available_execution_result=(
                                        _safe_collector_attr(
                                            evidence,
                                            "execution_result",
                                        )
                                        if evidence is not None
                                        else None
                                    ),
                                    duration_ns=(
                                        _safe_collector_attr(evidence, "duration_ns", 0)
                                        if evidence is not None
                                        else max(
                                            0,
                                            time.monotonic_ns() - fallback_started_ns,
                                        )
                                    ),
                                    missing=("transition_failed",),
                                )
                            raise
                        evidence = (
                            _safe_collector_method(
                                execution_trace,
                                "finish_execution",
                                observation=post_observation,
                                missing=("transition.execution_result",),
                            )
                            if execution_trace is not None and execution is not None
                            else None
                        )
                        post_source_bytes = (
                            _safe_collector_method(
                                execution_trace,
                                "source_screenshot_bytes",
                                post_observation.screenshot,
                                missing=("observation.screenshot.source_blob",),
                            )
                            if execution_trace is not None
                            else None
                        )
                        if execution is not None:
                            _safe_collector_method(
                                audit_capture,
                                "transition_completed",
                                post_observation=post_observation,
                                execution=execution,
                                execution_result=(
                                    _safe_collector_attr(evidence, "execution_result")
                                    if evidence is not None
                                    else None
                                ),
                                duration_ns=(
                                    _safe_collector_attr(evidence, "duration_ns", 0)
                                    if evidence is not None
                                    else max(
                                        0,
                                        time.monotonic_ns() - fallback_started_ns,
                                    )
                                ),
                                source_screenshot_bytes=post_source_bytes,
                                missing=("transition_completed",),
                            )
                        obs = post_observation

                    if terminate:
                        runtime_status = "completed"
                        break

                    if step >= max_step:
                        logger.debug("task steps reach max step, terminate")
                        runtime_status = "completed"
                        termination_source = "max_step"
                        break

            active_phase = "environment_evaluation"
            try:
                score, reason = env.get_task_score(task_type=task_name)
            except Exception as error:
                evaluation_exception = error
                raise
            logger.debug(f"task_score: {score}, reason: {reason}")
            traj_logger.log_score(score=score, reason=reason)

            active_phase = "teardown"
            teardown_attempted = True
            try:
                teardown_result = env.tear_down_task(task_type=task_name)
            except Exception as error:
                teardown_exception = error
                raise
            active_phase = "agent_done"
            agent.done()
            logger.debug(f"tear_down_task response: {teardown_result}")
            if runtime_status == "crashed":
                runtime_status = "completed"
            return step, score
        except Exception as error:
            runtime_status = "crashed"
            termination_source = active_phase
            termination_exception = error
            raise
        finally:
            try:
                if task_started:
                    _safe_collector_method(
                        audit_capture,
                        "end_task",
                        runtime_status=runtime_status,
                        termination_source=termination_source,
                        final_step_index=step,
                        termination_exception=termination_exception,
                        score=score,
                        reason=reason,
                        evaluation_exception=evaluation_exception,
                        teardown_attempted=teardown_attempted,
                        teardown_result=teardown_result,
                        teardown_exception=teardown_exception,
                        token_usage=latest_token_usage,
                        missing=("task_ended",),
                    )
            finally:
                _safe_collector_call(audit_stack.close)
                _safe_collector_call(audit_runtime_status_callback, runtime_status)


def _process_task_on_env(
    task_name: str,
    env_queue: Queue,
    agent_type: str,
    model_name: str,
    llm_base_url: str,
    api_key: str | None,
    log_file_root: str,
    max_step: int,
    retry_on_device_unhealthy: int = 2,
    enable_mcp: bool = False,
    *,
    audit_lifecycle: Any = None,
    audit_task_index: int = 1,
    audit_suite_family: str = "mobile_world",
    **kwargs,
) -> dict:
    """Process a single task on a specific environment.

    Args:
        task_name: Name of the task to execute
        env_url: URL of the environment to use
        agent_type: Type of agent to create
        model_name: Model name for the agent
        llm_base_url: LLM service base URL
        api_key: API key for LLM service
        log_file_root: Root directory for log files
        max_step: Maximum steps for task execution
        **kwargs: Additional kwargs for agent creation

    Returns:
        dict: Task result containing task_name, success, score, steps, duration_seconds
    """
    # Create thread-specific log file
    thread_id = threading.current_thread().ident
    thread_log_file = os.path.join(log_file_root, task_name, f"thread_{thread_id}.log")
    os.makedirs(os.path.dirname(thread_log_file), exist_ok=True)
    traj_logger = TrajLogger(log_file_root, task_name)

    def thread_filter(record):
        return record["extra"].get("thread_id") == thread_id

    thread_handler_id = logger.add(
        thread_log_file,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | container: {extra[container_name]} | {message}",
        level="DEBUG",
        enqueue=True,
        filter=thread_filter,
    )
    env, container_name = env_queue.get()

    try:
        with logger.contextualize(thread_id=thread_id, container_name=container_name):
            logger.info("Processing task '{}' on environment {}", task_name, env.base_url)
            audit_enabled = audit_lifecycle is not None and _safe_collector_call(
                bool,
                _safe_collector_attr(audit_lifecycle, "enabled", False),
                default=False,
            )
            if not audit_enabled:
                # Preserve the original feature-off control flow exactly.
                if enable_mcp:
                    assert isinstance(env, AndroidMCPEnvClient), (
                        f"env must be a AndroidMCPEnvClient, but got {type(env)}"
                    )
                    try:
                        env.reset_tools(task_type=task_name)
                    except Exception as e:
                        logger.exception(f"Error resetting tools for task {task_name}: {e}")
                        return None

                agent = create_agent(
                    agent_type,
                    model_name,
                    llm_base_url,
                    api_key,
                    env=env,
                    **kwargs,
                )
                task_start_time = time.time()
                while True:
                    try:
                        task_steps, task_score = _execute_single_task(
                            env,
                            agent,
                            task_name,
                            max_step,
                            traj_logger=traj_logger,
                            enable_mcp=enable_mcp,
                        )
                        break
                    except Exception as e:
                        if "Device is not healthy" in str(e) and retry_on_device_unhealthy > 0:
                            logger.warning("Device is not healthy, retrying...")
                            time.sleep(20)
                            retry_on_device_unhealthy -= 1
                            traj_logger.reset_traj()
                            continue
                        else:
                            logger.exception(f"Error executing task {task_name}")
                            return None
            else:
                requested_agent_metadata = {
                    "adapter": agent_type,
                    "model": model_name,
                    "configuration": {},
                }
                whole_task_attempt_index = 1
                binding = _safe_collector_method(
                    audit_lifecycle,
                    "start_task_attempt",
                    task_name=task_name,
                    task_index=audit_task_index,
                    suite_family=audit_suite_family,
                    agent=requested_agent_metadata,
                    environment=env,
                    whole_task_attempt_index=whole_task_attempt_index,
                )
                if enable_mcp:
                    assert isinstance(env, AndroidMCPEnvClient), (
                        f"env must be a AndroidMCPEnvClient, but got {type(env)}"
                    )
                    try:
                        env.reset_tools(task_type=task_name)
                    except Exception as error:
                        logger.exception(f"Error resetting tools for task {task_name}: {error}")
                        try:
                            if binding is not None:
                                _record_pre_goal_task_failure(
                                    _safe_collector_attr(binding, "capture"),
                                    _safe_collector_attr(binding, "metadata"),
                                    task_name=task_name,
                                    termination_source="mcp_reset_tools",
                                    error=error,
                                )
                        finally:
                            _safe_collector_method(
                                audit_lifecycle,
                                "finish_task_attempt",
                                binding=binding,
                                result=None,
                                exception=error,
                                retry_planned=False,
                                runtime_status="crashed",
                            )
                        return None

                try:
                    agent = create_agent(
                        agent_type,
                        model_name,
                        llm_base_url,
                        api_key,
                        env=env,
                        **kwargs,
                    )
                except Exception as error:
                    try:
                        if binding is not None:
                            _record_pre_goal_task_failure(
                                _safe_collector_attr(binding, "capture"),
                                _safe_collector_attr(binding, "metadata"),
                                task_name=task_name,
                                termination_source="agent_construction",
                                error=error,
                            )
                    finally:
                        _safe_collector_method(
                            audit_lifecycle,
                            "finish_task_attempt",
                            binding=binding,
                            result=None,
                            exception=error,
                            retry_planned=False,
                            runtime_status="crashed",
                        )
                    raise

                task_start_time = time.time()
                while True:
                    attempt_result: tuple[int, float] | None = None
                    attempt_exception: BaseException | None = None
                    attempt_runtime_status: str | None = None
                    retry_planned = False

                    def remember_runtime_status(runtime_status: str) -> None:
                        nonlocal attempt_runtime_status
                        attempt_runtime_status = runtime_status

                    try:
                        task_steps, task_score = _execute_single_task(
                            env,
                            agent,
                            task_name,
                            max_step,
                            traj_logger=traj_logger,
                            enable_mcp=enable_mcp,
                            audit_capture=(
                                _safe_collector_attr(binding, "capture")
                                if binding is not None
                                else None
                            ),
                            audit_metadata=(
                                _safe_collector_attr(binding, "metadata")
                                if binding is not None
                                else None
                            ),
                            audit_runtime_status_callback=remember_runtime_status,
                        )
                        attempt_result = (task_steps, task_score)
                    except Exception as error:
                        attempt_exception = error
                        retry_planned = (
                            "Device is not healthy" in str(error) and retry_on_device_unhealthy > 0
                        )
                        if not retry_planned:
                            logger.exception(f"Error executing task {task_name}")
                    finally:
                        _safe_collector_method(
                            audit_lifecycle,
                            "finish_task_attempt",
                            binding=binding,
                            result=attempt_result,
                            exception=attempt_exception,
                            retry_planned=retry_planned,
                            runtime_status=(
                                attempt_runtime_status
                                or (
                                    "completed"
                                    if attempt_result is not None and attempt_exception is None
                                    else "crashed"
                                )
                            ),
                        )

                    if attempt_exception is None:
                        break
                    if not retry_planned:
                        return None

                    logger.warning("Device is not healthy, retrying...")
                    time.sleep(20)
                    retry_on_device_unhealthy -= 1
                    whole_task_attempt_index += 1
                    traj_logger.reset_traj()
                    binding = _safe_collector_method(
                        audit_lifecycle,
                        "start_task_attempt",
                        task_name=task_name,
                        task_index=audit_task_index,
                        suite_family=audit_suite_family,
                        agent=requested_agent_metadata,
                        environment=env,
                        whole_task_attempt_index=whole_task_attempt_index,
                    )

            task_duration = time.time() - task_start_time
            task_success = task_score > 0.0

            logger.info(
                "Task '{}' completed on {}: success={}, score={}, steps={}, duration={:.1f}s",
                task_name,
                env.base_url,
                task_success,
                task_score,
                task_steps,
                task_duration,
            )

            return {
                "task_name": task_name,
                "score": task_score,
            }
    finally:
        # Remove the thread-specific handler
        logger.remove(thread_handler_id)
        env_queue.put((env, container_name))


def _init_env(
    env_url: str, device: str, step_wait_time: float, suite_family: str, enable_mcp: bool
) -> AndroidEnvClient:
    """Initialize the environment."""
    if enable_mcp:
        env = AndroidMCPEnvClient(env_url, device, step_wait_time=step_wait_time)
    else:
        env = AndroidEnvClient(env_url, device, step_wait_time=step_wait_time)
    env.switch_suite_family(suite_family)
    return env


def run_agent_with_evaluation(
    agent_type: str,
    model_name: str,
    llm_base_url: str,
    log_file_root: str,
    tasks: list[str],
    max_step: int = -1,
    aw_urls: list[str] | None = None,
    api_key: str | None = None,
    device: str = "emulator-5554",
    step_wait_time: float = 1.0,
    suite_family: str = "mobile_world",
    env_name_prefix: str = "mobile_world_env",
    env_image: str = "mobile_world",
    dry_run: bool = False,
    enable_mcp: bool = False,
    enable_user_interaction: bool = False,
    max_concurrency: int | None = None,
    shuffle_tasks: bool = False,
    auto_retry: int = 10,
    audit_lifecycle: Any = None,
    **kwargs,
) -> list[dict]:
    """Run the agent and return the evaluation results.

    Args:
        agent_type: Type of agent to use
        model_name: Model name for the agent
        llm_base_url: LLM service base URL
        log_file_root: Root directory for log files
        tasks: List of task names to execute (empty list for all tasks)
        max_step: Maximum steps for task execution
        aw_urls: List of Android World backend URLs. If None, auto-discover from containers
        api_key: API key for LLM service
        device: Android device ID
        step_wait_time: Wait time after each step
        suite_family: Suite family to use
        **kwargs: Additional kwargs for agent creation

    Returns:
        list[dict]: The evaluation results for each task, containing task_name, success, score, steps, duration_seconds, env_url
    """

    container_names = None
    if aw_urls is None or len(aw_urls) == 0:
        logger.info("No backend URLs specified, auto-discovering from containers...")
        aw_urls, container_names = discover_backends(image_filter=env_image, prefix=env_name_prefix)
        logger.info("Container names: {}", container_names)
        if not aw_urls:
            logger.error("No backend URLs found. Please start containers or specify --aw-host")
            return [], []

    logger.info("Using {} backend URL(s): {}", len(aw_urls), aw_urls)

    envs = Parallel(
        n_jobs=min(max_concurrency if max_concurrency is not None else len(aw_urls), len(aw_urls)),
        backend="threading",
    )(
        delayed(_init_env)(env_url, device, step_wait_time, suite_family, enable_mcp)
        for env_url in aw_urls
    )

    if len(tasks) != 0:
        task_list = tasks
    else:
        task_list = envs[0].get_suite_task_list(
            enable_mcp=enable_mcp, enable_user_interaction=enable_user_interaction
        )

    logger.info("Task list: {} ({} tasks)", task_list, len(task_list))

    # Stable original 1-based indices survive pending filtering, shuffle, and
    # whole-task device retries.  Duplicate names already share runner result
    # identity, so retain the first occurrence deterministically.
    audit_task_indices: dict[str, int] = {}
    if audit_lifecycle is not None and _safe_collector_call(
        bool,
        _safe_collector_attr(audit_lifecycle, "enabled", False),
        default=False,
    ):
        for task_index, task_name in enumerate(task_list, start=1):
            audit_task_indices.setdefault(task_name, task_index)

    num_envs = len(envs)
    max_attempts = min(1 + auto_retry, 10)  # Cap at 10 to prevent infinite loops

    for attempt in range(max_attempts):
        # Scan finished tasks each iteration (picks up results from previous attempts)
        finished_task_list, finished_scores = scan_finished_tasks(log_file_root, task_list)
        logger.info(
            "Finished task list: {} ({} tasks)", finished_task_list, len(finished_task_list)
        )

        pending_tasks = [task for task in task_list if task not in finished_task_list]
        logger.info(
            "Attempt {}/{}: {} remaining tasks to execute",
            attempt + 1,
            max_attempts,
            len(pending_tasks),
        )

        if not pending_tasks:
            logger.info("All tasks finished, no retry needed")
            break

        env_queue = Queue[tuple[AndroidEnvClient, str | None]](maxsize=num_envs)
        for i, env in enumerate(envs):
            env_queue.put((env, container_names[i] if container_names else None))

        if shuffle_tasks:
            random.shuffle(pending_tasks)

        if not dry_run:
            task_results = Parallel(
                n_jobs=min(max_concurrency if max_concurrency is not None else num_envs, num_envs),
                backend="threading",
            )(
                delayed(_process_task_on_env)(
                    task_name=task_name,
                    env_queue=env_queue,
                    agent_type=agent_type,
                    model_name=model_name,
                    llm_base_url=llm_base_url,
                    api_key=api_key,
                    log_file_root=log_file_root,
                    max_step=max_step,
                    enable_mcp=enable_mcp,
                    audit_lifecycle=audit_lifecycle,
                    audit_task_index=audit_task_indices.get(task_name, 1),
                    audit_suite_family=suite_family,
                    **kwargs,
                )
                for task_name in pending_tasks
            )
        else:
            logger.info("Dry run mode, skipping task execution")
            task_results = []
            break

        # Identify failed tasks for potential retry
        failed_this_round = [
            task_name
            for task_name, task_result in zip(pending_tasks, task_results)
            if task_result is None
        ]

        logger.info(
            "Attempt {}/{} done: {} succeeded, {} failed/stale",
            attempt + 1,
            max_attempts,
            len(pending_tasks) - len(failed_this_round),
            len(failed_this_round),
        )

        if not failed_this_round or attempt >= max_attempts - 1:
            break

        logger.info(
            "Auto-retrying {} failed tasks (retry {}/{})",
            len(failed_this_round),
            attempt + 1,
            auto_retry,
        )

    # Final scan to get all finished results (including from retries)
    finished_task_list, finished_scores = scan_finished_tasks(log_file_root, task_list)
    # Build final results from scan (authoritative source)
    success_task_results = []
    for task_name, score in zip(finished_task_list, finished_scores):
        success_task_results.append({"task_name": task_name, "score": score})

    task_list_with_no_results = [task for task in task_list if task not in finished_task_list]
    logger.info(
        f"Final: {len(success_task_results)} tasks with results, {len(task_list_with_no_results)} with no results"
    )

    return (success_task_results, task_list_with_no_results)
