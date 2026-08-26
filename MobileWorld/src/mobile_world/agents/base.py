"""
Base agent interface for mobile automation.
"""

import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any

from loguru import logger
from openai import OpenAI

from mobile_world.runtime.utils.models import JSONAction

_AUDIT_VALUE_UNSET = object()


class BaseAgent(ABC):
    """Abstract base class for all mobile automation agents."""

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        self._total_completion_tokens: int = 0
        self._total_prompt_tokens: int = 0
        self._total_cached_tokens: int = 0

    def initialize(self, instruction: str) -> bool:
        """Initialize the agent with the given instruction."""
        self.instruction = instruction
        logger.debug(f"initialized the agent with the given instruction: {self.instruction}")
        self.initialize_hook(self.instruction)
        return True

    def initialize_hook(self, instruction: str) -> None:
        """Hook for initializing the agent."""
        pass

    @abstractmethod
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        """Generate the next action based on current observation."""
        raise NotImplementedError("predict method is not implemented")

    def done(self) -> None:
        """finalize the agent for the current task."""
        logger.debug(f"finalizing the agent for the current task: {self.instruction}")
        self.instruction = None
        self.reset()

    def reset(self) -> None:
        """Reset the agent for the next task."""
        logger.warning(
            "reset method is not implemented, note the agent memory will be carried over to the next task"
        )
        pass

    def build_openai_client(self, base_url: str, api_key: str) -> None:
        """Build the OpenAI client."""
        self.openai_client = OpenAI(
            base_url=base_url,
            api_key=api_key if api_key else "empty",
            timeout=120.0,
        )
        logger.debug(f"built the OpenAI client with base_url={base_url}")

    def _wrap_stream_with_usage_logging(self, stream: Any) -> Any:
        """Wrap a streaming response to log usage when stream completes."""
        final_usage = None
        for chunk in stream:
            if hasattr(chunk, "usage") and chunk.usage is not None:
                final_usage = chunk
            yield chunk

        if final_usage is not None:
            self._log_openai_usage(final_usage)

    def openai_chat_completions_create(
        self,
        model: str,
        messages: list[dict],
        retry_times: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> str | None:
        if stream:
            # Enable usage reporting in stream
            kwargs.setdefault("stream_options", {})
            kwargs["stream_options"]["include_usage"] = True
            audit_call = self._begin_actor_model_audit_call()
            audit_attempt = None
            if audit_call is not None:
                sdk_arguments = {"model": model, "messages": messages, **kwargs, "stream": True}
                audit_attempt = self._begin_model_audit_attempt(
                    audit_call,
                    sdk_arguments,
                    stream=True,
                )
            try:
                response = self.openai_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **kwargs,
                    stream=True,
                )
            except Exception as error:
                self._record_model_audit_failure(
                    audit_attempt,
                    error,
                    failure_phase="provider_call",
                    retry_planned=self._model_audit_adapter_retry_planned(audit_attempt),
                )
                raise
            wrapped = self._wrap_model_audit_stream(
                audit_attempt,
                response,
                self._log_openai_usage,
            )
            if wrapped is not None:
                return wrapped
            return self._wrap_stream_with_usage_logging(response)

        audit_call = None
        audit_call_initialized = False
        while retry_times > 0:
            try:
                if "claude" in model:
                    kwargs["max_tokens"] = 64000
                    del kwargs["temperature"]

                if "gpt" in model.lower() or "o1" in model.lower():
                    if "max_tokens" in kwargs:
                        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")

                if "kimi-k" in model.lower():
                    kwargs["extra_body"] = {"enable_thinking": True}

            except Exception as error:
                if self._handle_openai_error(error, kwargs):
                    continue
                retry_times -= 1
                time.sleep(1)
                continue

            if not audit_call_initialized:
                audit_call = self._begin_actor_model_audit_call()
                audit_call_initialized = True
            audit_attempt = None
            if audit_call is not None:
                sdk_arguments = {"model": model, "messages": messages, **kwargs}
                audit_attempt = self._begin_model_audit_attempt(
                    audit_call,
                    sdk_arguments,
                    stream=False,
                )

            provider_returned = False
            response = None
            try:
                response = self.openai_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **kwargs,
                )
                provider_returned = True

                self._log_openai_usage(response)
                final_content = response.choices[0].message.content.strip()
                # for k2.5, we keep its reasoning_content
                if (
                    "kimi-k" in model.lower()
                    and hasattr(response.choices[0].message, "reasoning_content")
                    and response.choices[0].message.reasoning_content
                ):
                    final_content = f"<think>{response.choices[0].message.reasoning_content.strip()}</think>\n{final_content}"
            except Exception as error:
                error_msg = str(error)
                retry_planned = (
                    (
                        "max_tokens" in error_msg
                        and "max_completion_tokens" in error_msg
                        and "max_tokens" in kwargs
                    )
                    or retry_times > 1
                    or self._model_audit_adapter_retry_planned(audit_attempt)
                )
                self._record_model_audit_failure(
                    audit_attempt,
                    error,
                    failure_phase=(
                        "response_serialization" if provider_returned else "provider_call"
                    ),
                    retry_planned=retry_planned,
                    raw_response=response if provider_returned else _AUDIT_VALUE_UNSET,
                )

                logger.warning(f"Error calling OpenAI API: {error}")

                # Check if error is about max_tokens parameter and retry with max_completion_tokens
                if "max_tokens" in error_msg and "max_completion_tokens" in error_msg:
                    if "max_tokens" in kwargs:
                        logger.info("Retrying with max_completion_tokens instead of max_tokens")
                        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                        continue  # Retry immediately without decrementing retry_times

                retry_times -= 1
                time.sleep(1)
                continue

            self._record_model_audit_response(audit_attempt, response, final_content)
            return final_content
        return None

    @staticmethod
    def _handle_openai_error(error: Exception, kwargs: dict[str, Any]) -> bool:
        """Preserve the original pre-invocation error logging behavior."""

        error_msg = str(error)
        logger.warning(f"Error calling OpenAI API: {error}")
        if "max_tokens" in error_msg and "max_completion_tokens" in error_msg:
            if "max_tokens" in kwargs:
                logger.info("Retrying with max_completion_tokens instead of max_tokens")
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                return True
        return False

    def _begin_actor_model_audit_call(self) -> Any:
        """Lazily enter audit capture only when an enabled task context exists."""

        try:
            from mobile_world.runtime.audit.context import get_audit_context

            context = get_audit_context()
            if context is None or not getattr(context.recorder, "enabled", False):
                return None

            from mobile_world.runtime.audit.model_io import begin_model_call

            return begin_model_call(
                call_role="actor",
                component=type(self).__module__,
                client=self.openai_client,
            )
        except Exception:
            # Collector setup must never prevent or replace a provider call.
            return None

    @staticmethod
    def _begin_model_audit_attempt(
        audit_call: Any,
        sdk_arguments: dict[str, Any],
        *,
        stream: bool,
    ) -> Any:
        """Enter an audit attempt without allowing collector failures to escape."""

        if audit_call is None:
            return None
        try:
            return audit_call.begin_attempt(sdk_arguments, stream=stream)
        except Exception:
            return None

    @staticmethod
    def _model_audit_adapter_retry_planned(audit_attempt: Any) -> bool:
        if audit_attempt is None:
            return False
        try:
            return bool(audit_attempt.adapter_retry_planned)
        except Exception:
            return False

    @staticmethod
    def _record_model_audit_failure(
        audit_attempt: Any,
        error: Exception,
        *,
        failure_phase: str,
        retry_planned: bool,
        raw_response: Any = _AUDIT_VALUE_UNSET,
    ) -> None:
        if audit_attempt is None:
            return
        try:
            if raw_response is _AUDIT_VALUE_UNSET:
                audit_attempt.record_failure(
                    error,
                    failure_phase=failure_phase,
                    retry_planned=retry_planned,
                )
            else:
                audit_attempt.record_failure(
                    error,
                    failure_phase=failure_phase,
                    retry_planned=retry_planned,
                    raw_response=raw_response,
                )
        except Exception:
            return

    @staticmethod
    def _record_model_audit_response(
        audit_attempt: Any,
        response: Any,
        returned_value: Any = _AUDIT_VALUE_UNSET,
    ) -> None:
        if audit_attempt is None:
            return
        try:
            if returned_value is _AUDIT_VALUE_UNSET:
                audit_attempt.record_nonstream_response(response)
            else:
                audit_attempt.record_nonstream_response(response, returned_value)
        except Exception:
            return

    @staticmethod
    def _wrap_model_audit_stream(
        audit_attempt: Any,
        response: Any,
        usage_callback: Any,
    ) -> Any:
        if audit_attempt is None:
            return None
        try:
            if audit_attempt.request_recorded:
                return audit_attempt.wrap_stream(response, usage_callback)
        except Exception:
            return None
        return None

    @staticmethod
    def _begin_outer_model_audit_retry_group() -> tuple[Any, str] | None:
        """Return enabled outer-retry correlation without exposing collector faults."""

        try:
            from mobile_world.runtime.audit.context import get_audit_context

            audit_context = get_audit_context()
            if audit_context is None or not getattr(audit_context.recorder, "enabled", False):
                return None
            retry_group_id = audit_context.retry_group_id
            if retry_group_id is None:
                from mobile_world.runtime.audit.ids import new_ulid

                retry_group_id = new_ulid()
            return audit_context, retry_group_id
        except Exception:
            # Correlation is optional evidence; the original adapter path wins.
            return None

    @staticmethod
    @contextmanager
    def _outer_model_audit_attempt_scope(
        retry_group: tuple[Any, str],
        *,
        adapter_attempt_index: int,
        adapter_retry_planned: bool,
    ) -> Any:
        """Bind one optional attempt without changing provider exception semantics.

        Collector setup/enter/exit failures are swallowed.  The provider block
        is yielded exactly once, and an exception raised by that block is always
        re-raised unchanged even if context cleanup also fails.
        """

        manager = None
        entered = False
        try:
            audit_context, retry_group_id = retry_group
            attempt_context = audit_context.derive(
                model_call_id=None,
                retry_group_id=retry_group_id,
                adapter_attempt_index=adapter_attempt_index,
                adapter_retry_planned=adapter_retry_planned,
            )
            from mobile_world.runtime.audit.context import bind_audit_context

            manager = bind_audit_context(attempt_context)
            manager.__enter__()
            entered = True
        except Exception as setup_error:
            # A custom/fault-injected manager may fail after partially entering.
            # Give it one cleanup chance, then run the provider under the parent
            # context; never retry the provider as a collector fallback.
            if manager is not None:
                try:
                    manager.__exit__(type(setup_error), setup_error, setup_error.__traceback__)
                except Exception:
                    pass
            manager = None

        try:
            yield
        except BaseException as provider_error:
            if entered and manager is not None:
                try:
                    manager.__exit__(
                        type(provider_error),
                        provider_error,
                        provider_error.__traceback__,
                    )
                except Exception:
                    pass
            raise
        else:
            if entered and manager is not None:
                try:
                    manager.__exit__(None, None, None)
                except Exception:
                    pass

    def _log_openai_usage(self, response: Any) -> None:
        """Log and track the usage of the OpenAI API."""
        if response.usage is None:
            return

        completion_tokens = response.usage.completion_tokens or 0
        prompt_tokens = response.usage.prompt_tokens or 0
        cached_tokens = 0

        if (
            hasattr(response.usage, "prompt_tokens_details")
            and response.usage.prompt_tokens_details
        ):
            cached_tokens = response.usage.prompt_tokens_details.cached_tokens or 0

        self._total_completion_tokens += completion_tokens
        self._total_prompt_tokens += prompt_tokens
        self._total_cached_tokens += cached_tokens

        logger.debug(
            f"OpenAI API usage: completion={completion_tokens}, prompt={prompt_tokens}, "
            f"cached={cached_tokens} | Total: completion={self._total_completion_tokens}, "
            f"prompt={self._total_prompt_tokens}, cached={self._total_cached_tokens}"
        )

    def get_total_token_usage(self) -> dict[str, int]:
        """Get the total token usage across all API calls."""
        return {
            "completion_tokens": self._total_completion_tokens,
            "prompt_tokens": self._total_prompt_tokens,
            "cached_tokens": self._total_cached_tokens,
            "total_tokens": self._total_completion_tokens + self._total_prompt_tokens,
        }

    def reset_token_usage(self) -> None:
        """Reset the token usage counters."""
        self._total_completion_tokens = 0
        self._total_prompt_tokens = 0
        self._total_cached_tokens = 0


class MCPAgent(BaseAgent):
    def __init__(
        self,
        tools: list[dict],
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.tools = tools

    def initialize(self, instruction: str) -> bool:
        """Initialize the agent with the given instruction."""
        self.instruction = instruction

        self.initialize_hook(self.instruction)
        logger.debug(f"initialized the agent with the given instruction: {self.instruction}")
        return True

    def reset_tools(self, tools: list[dict]) -> None:
        """Reset the tools for the agent."""
        self.tools = tools

    @abstractmethod
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        """Generate the next action based on current observation."""
        raise NotImplementedError("predict method is not implemented")
