"""
Base agent interface for mobile automation.
"""

import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, cast

from loguru import logger
from openai import OpenAI

from mobile_world.runtime.utils.models import JSONAction

_AUDIT_VALUE_UNSET = object()


class BaseAgent(ABC):
    """Abstract base class for all mobile automation agents."""

    sentinel_host_id: str | None = None
    sentinel_history_codec_id: str | None = None

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ):
        self._total_completion_tokens: int = 0
        self._total_prompt_tokens: int = 0
        self._total_cached_tokens: int = 0
        self.instruction: str | None = None
        self._prompt_sentinel = kwargs.get("prompt_sentinel")
        self._last_prompt_sentinel_logical_call: Any | None = None
        self._sentinel_host_id = (
            kwargs.get("sentinel_host_id")
            or self.sentinel_host_id
            or (f"{type(self).__module__}.{type(self).__qualname__}")
        )
        self._sentinel_history_codec_id = (
            kwargs.get("sentinel_history_codec_id") or self.sentinel_history_codec_id
        )

    def initialize(self, instruction: str) -> bool:
        """Initialize the agent with the given instruction."""
        self.instruction = instruction
        if self._production_safe_logging_active():
            logger.debug("Initialized production actor instruction")
        else:
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
        if self._production_safe_logging_active():
            logger.debug("Finalizing production actor task")
        else:
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

    def _production_logical_call(self) -> Any | None:
        """Return only the ambient strict production call, never a caller flag."""

        try:
            from mobile_world.runtime.sentinel import current_sentinel_logical_call

            logical_call = current_sentinel_logical_call()
        except Exception:
            return None
        if logical_call is None or not bool(
            getattr(logical_call, "requires_strict_provider_audit", False)
        ):
            return None
        return logical_call

    def _production_safe_logging_active(self) -> bool:
        """Whether raw prompt/response/thinking logs must be suppressed."""

        if self._production_logical_call() is not None:
            return True
        sentinel = self._prompt_sentinel
        return sentinel is not None and bool(getattr(sentinel, "strict_provider_audit", False))

    def _production_dispatch_client(self) -> Any:
        """Clamp an exact production OpenAI client at every physical dispatch."""

        logical_call = self._production_logical_call()
        if logical_call is None:
            return self.openai_client
        context = getattr(logical_call, "context", None)
        attributes = getattr(context, "attributes", None)
        if type(attributes) is not dict or "r24_case_deadline_monotonic_ns" not in attributes:
            # CPU audit fixtures intentionally exercise strict detail without a
            # live case lease. A real live call always carries this attribute.
            return self.openai_client
        deadline_ns = attributes["r24_case_deadline_monotonic_ns"]
        if type(deadline_ns) is not int:
            raise RuntimeError("production actor deadline binding is invalid")
        remaining_ns = deadline_ns - time.monotonic_ns()
        if remaining_ns <= 0:
            raise TimeoutError("production actor deadline elapsed")
        if type(self.openai_client) is not OpenAI:
            raise RuntimeError("production actor requires the exact OpenAI client")
        return self.openai_client.with_options(
            timeout=max(0.001, min(120.0, remaining_ns / 1_000_000_000))
        )

    def _require_production_response_model(self, response: Any, requested_model: str) -> None:
        if self._production_logical_call() is None:
            return
        returned_model = getattr(response, "model", None)
        if type(returned_model) is not str or returned_model != requested_model:
            raise RuntimeError("production actor returned model differs from served model")

    def _bounded_provider_retry_sleep(self, seconds: float) -> None:
        """Keep retry backoff inside the active production case deadline."""

        logical_call = self._production_logical_call()
        if logical_call is None:
            time.sleep(seconds)
            return
        context = getattr(logical_call, "context", None)
        attributes = getattr(context, "attributes", None)
        deadline_ns = (
            attributes.get("r24_case_deadline_monotonic_ns") if type(attributes) is dict else None
        )
        if type(deadline_ns) is not int:
            time.sleep(seconds)
            return
        remaining_seconds = max(0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        if remaining_seconds > 0:
            time.sleep(min(seconds, remaining_seconds))

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
        call_role: str = "actor",
        **kwargs: Any,
    ) -> Any:
        response: Any
        if stream:
            # Enable usage reporting in stream
            kwargs.setdefault("stream_options", {})
            kwargs["stream_options"]["include_usage"] = True
            provider_model, provider_messages, provider_kwargs = (
                self._apply_prompt_sentinel_before_model_call(
                    model=model,
                    messages=messages,
                    kwargs=kwargs,
                    stream=True,
                    call_role=call_role,
                )
            )
            audit_call = self._begin_actor_model_audit_call(call_role=call_role)
            audit_attempt = None
            sdk_arguments = {
                "model": provider_model,
                "messages": provider_messages,
                **provider_kwargs,
                "stream": True,
            }
            if audit_call is not None:
                audit_attempt = self._begin_model_audit_attempt(
                    audit_call,
                    sdk_arguments,
                    stream=True,
                )
            provider_started_ns = time.monotonic_ns()
            dispatch_started = False
            try:
                self._bind_prompt_sentinel_actor_sdk_arguments(
                    sdk_arguments, stream=True, audit_attempt=audit_attempt
                )
                dispatch_client = self._production_dispatch_client()
                dispatch_started = True
                response = dispatch_client.chat.completions.create(
                    model=provider_model,
                    messages=cast(Any, provider_messages),
                    **provider_kwargs,
                    stream=True,
                )
                self._require_production_response_model(response, provider_model)
            except Exception as error:
                self._record_model_audit_failure(
                    audit_attempt,
                    error,
                    failure_phase="provider_call",
                    retry_planned=self._model_audit_adapter_retry_planned(audit_attempt),
                )
                if not dispatch_started:
                    from mobile_world.runtime.sentinel.r2_4.production_audit import (
                        ProductionRuntimeAuditError,
                    )

                    if isinstance(error, ProductionRuntimeAuditError):
                        raise
                if dispatch_started:
                    self._record_prompt_sentinel_provider_attempt(
                        started_ns=provider_started_ns,
                        succeeded=False,
                        provider_model=provider_model,
                        audit_attempt=audit_attempt,
                    )
                raise
            self._record_prompt_sentinel_provider_attempt(
                started_ns=provider_started_ns,
                succeeded=True,
                provider_model=provider_model,
                response=response,
                audit_attempt=audit_attempt,
            )
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
        provider_model = model
        provider_messages = messages
        provider_kwargs = kwargs
        sentinel_request_initialized = False
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
                # Preserve the host's established one-second delay after every
                # ordinary failed attempt, including the final attempt.  A
                # production-authorized call skips an otherwise pointless
                # final delay so it cannot consume time after its last bounded
                # dispatch.
                if retry_times > 0 or self._production_logical_call() is None:
                    self._bounded_provider_retry_sleep(1.0)
                continue

            if not sentinel_request_initialized:
                provider_model, provider_messages, provider_kwargs = (
                    self._apply_prompt_sentinel_before_model_call(
                        model=model,
                        messages=messages,
                        kwargs=kwargs,
                        stream=False,
                        call_role=call_role,
                    )
                )
                sentinel_request_initialized = True
            if not audit_call_initialized:
                audit_call = self._begin_actor_model_audit_call(call_role=call_role)
                audit_call_initialized = True
            audit_attempt = None
            sdk_arguments = {
                "model": provider_model,
                "messages": provider_messages,
                **provider_kwargs,
            }
            if audit_call is not None:
                audit_attempt = self._begin_model_audit_attempt(
                    audit_call,
                    sdk_arguments,
                    stream=False,
                )

            provider_returned = False
            dispatch_started = False
            response = None
            provider_started_ns = time.monotonic_ns()
            try:
                self._bind_prompt_sentinel_actor_sdk_arguments(
                    sdk_arguments, stream=False, audit_attempt=audit_attempt
                )
                dispatch_client = self._production_dispatch_client()
                dispatch_started = True
                response = dispatch_client.chat.completions.create(
                    model=provider_model,
                    messages=cast(Any, provider_messages),
                    **provider_kwargs,
                )
                provider_returned = True
                self._require_production_response_model(response, provider_model)

                self._log_openai_usage(response)
                response_content = response.choices[0].message.content
                if not isinstance(response_content, str):
                    raise TypeError("OpenAI response content must be a string")
                final_content = response_content.strip()
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
                        and "max_tokens" in provider_kwargs
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
                if not dispatch_started:
                    from mobile_world.runtime.sentinel.r2_4.production_audit import (
                        ProductionRuntimeAuditError,
                    )

                    if isinstance(error, ProductionRuntimeAuditError):
                        raise
                if dispatch_started:
                    self._record_prompt_sentinel_provider_attempt(
                        started_ns=provider_started_ns,
                        succeeded=False,
                        provider_model=provider_model,
                        response=response,
                        audit_attempt=audit_attempt,
                    )

                if self._production_safe_logging_active():
                    logger.warning("Production actor provider attempt failed")
                else:
                    logger.warning(f"Error calling OpenAI API: {error}")

                if not dispatch_started and isinstance(error, TimeoutError):
                    retry_times = 0
                    continue

                # Check if error is about max_tokens parameter and retry with max_completion_tokens
                if "max_tokens" in error_msg and "max_completion_tokens" in error_msg:
                    if "max_tokens" in provider_kwargs:
                        logger.info("Retrying with max_completion_tokens instead of max_tokens")
                        provider_kwargs["max_completion_tokens"] = provider_kwargs.pop("max_tokens")
                        continue  # Retry immediately without decrementing retry_times

                retry_times -= 1
                if retry_times > 0 or self._production_logical_call() is None:
                    self._bounded_provider_retry_sleep(1.0)
                continue

            self._record_model_audit_response(audit_attempt, response, final_content)
            self._record_prompt_sentinel_provider_attempt(
                started_ns=provider_started_ns,
                succeeded=True,
                provider_model=provider_model,
                response=response,
                audit_attempt=audit_attempt,
            )
            return final_content
        return None

    @contextmanager
    def _sentinel_logical_call_scope(
        self,
        *,
        call_role: str = "actor",
        attributes: dict[str, Any] | None = None,
    ) -> Any:
        """Bind one short-lived Sentinel cache after host prompt assembly.

        No Sentinel ID or import is created when the feature is not configured.
        The scope is independent from Collector model-call/retry-group identity.
        """

        sentinel = self._prompt_sentinel
        if sentinel is None:
            yield None
            return
        try:
            from mobile_world.runtime.sentinel import (
                SentinelCallRole,
                bind_sentinel_logical_call,
                current_sentinel_logical_call,
            )

            role = SentinelCallRole(call_role)
            current = current_sentinel_logical_call()
            if current is not None and current.matches(
                sentinel,
                host_id=self._sentinel_host_id,
                history_codec_id=self._sentinel_history_codec_id,
                call_role=role,
            ):
                call = current
                manager = None
            else:
                trusted_attributes = {} if attributes is None else attributes
                from mobile_world.runtime.sentinel.r2_4.live_policy import (
                    OwnerAuthorizedLivePerCallPolicyV1,
                )

                policy = sentinel.policy
                if type(policy) is OwnerAuthorizedLivePerCallPolicyV1:
                    # Caller dictionaries are not task authority.  The exact
                    # production policy derives these attributes only from the
                    # driver's active Collector task binding.
                    trusted_attributes = policy.current_case_context_attributes()
                call = sentinel.logical_call(
                    host_id=self._sentinel_host_id,
                    history_codec_id=self._sentinel_history_codec_id,
                    call_role=role,
                    attributes=trusted_attributes,
                )
                manager = bind_sentinel_logical_call(call)
        except Exception:
            if sentinel.strict_provider_audit:
                raise
            logger.warning("Prompt Sentinel scope setup failed open to Original")
            yield None
            return
        if manager is None:
            yield call
        else:
            with manager:
                yield call

    def _apply_prompt_sentinel_before_model_call(
        self,
        *,
        model: str,
        messages: list[dict],
        kwargs: dict[str, Any],
        stream: bool,
        call_role: str,
    ) -> tuple[str, list[dict], dict[str, Any]]:
        """Select Original or one cached validated final request for the SDK."""

        sentinel = self._prompt_sentinel
        if sentinel is None:
            return model, messages, kwargs
        try:
            from mobile_world.runtime.sentinel import (
                SentinelCallRole,
                current_sentinel_logical_call,
            )

            role = SentinelCallRole(call_role)
            request: dict[str, Any] = {"model": model, "messages": messages, **kwargs}
            if stream:
                request["stream"] = True
            logical_call = current_sentinel_logical_call()
            if logical_call is None or not logical_call.matches(
                sentinel,
                host_id=self._sentinel_host_id,
                history_codec_id=self._sentinel_history_codec_id,
                call_role=role,
            ):
                logical_call = sentinel.logical_call(
                    host_id=self._sentinel_host_id,
                    history_codec_id=self._sentinel_history_codec_id,
                    call_role=role,
                )
            result = logical_call.before_model_call(request)
            # OFF, SHADOW, recursion bypass, kill switch, and every fallback
            # preserve the original provider argument objects and identities.
            if not result.use_transformed_request:
                return model, messages, kwargs
            final = result.final_request
            if not isinstance(final, dict):
                return model, messages, kwargs
            final_model = final.get("model")
            final_messages = final.get("messages")
            if not isinstance(final_model, str) or not isinstance(final_messages, list):
                return model, messages, kwargs
            final_kwargs = {
                key: value for key, value in final.items() if key not in {"model", "messages"}
            }
            if stream:
                if final_kwargs.pop("stream", None) is not True:
                    return model, messages, kwargs
            elif "stream" in final_kwargs:
                return model, messages, kwargs
            return (
                final_model,
                cast(list[dict], final_messages),
                cast(dict[str, Any], final_kwargs),
            )
        except Exception:
            if bool(getattr(sentinel, "strict_provider_audit", False)):
                raise
            # Sentinel must never turn its own setup/serialization fault into an
            # actor-provider outage. Typed runtime failures are handled inside
            # PromptSentinel; this is the final integration backstop.
            logger.warning("Prompt Sentinel failed open to Original")
            return model, messages, kwargs

    def _bind_prompt_sentinel_actor_sdk_arguments(
        self,
        sdk_arguments: dict[str, Any],
        *,
        stream: bool,
        audit_attempt: Any,
    ) -> None:
        """Bind exact SDK kwargs as the final operation before live dispatch."""

        from mobile_world.runtime.sentinel import current_sentinel_logical_call

        logical_call = current_sentinel_logical_call()
        if logical_call is None:
            return
        logical_call.bind_actor_sdk_arguments(
            sdk_arguments,
            stream=stream,
            collector_request_locator=getattr(audit_attempt, "request_artifact_locator", None),
        )

    def _record_prompt_sentinel_provider_attempt(
        self,
        *,
        started_ns: int,
        succeeded: bool,
        provider_model: str,
        response: Any = None,
        audit_attempt: Any = None,
    ) -> None:
        """Record hash-safe retry metadata on the current Sentinel logical call."""

        logical_call = None
        try:
            from mobile_world.runtime.sentinel import current_sentinel_logical_call

            logical_call = current_sentinel_logical_call()
            if logical_call is None:
                return
            response_id = getattr(response, "id", None)
            response_model = getattr(response, "model", None)
            choices = getattr(response, "choices", None)
            first_choice = choices[0] if isinstance(choices, list) and choices else None
            finish_reason = getattr(first_choice, "finish_reason", None)
            usage = getattr(response, "usage", None)

            def optional_text(value: Any) -> str | None:
                return value if type(value) is str else None

            def optional_count(value: Any) -> int | None:
                return value if type(value) is int and value >= 0 else None

            logical_call.record_actor_provider_attempt(
                latency_ns=max(0, time.monotonic_ns() - started_ns),
                succeeded=succeeded,
                response_id=optional_text(response_id),
                model_id=optional_text(response_model) or optional_text(provider_model),
                finish_reason=optional_text(finish_reason),
                input_tokens=optional_count(getattr(usage, "prompt_tokens", None)),
                output_tokens=optional_count(getattr(usage, "completion_tokens", None)),
                total_tokens=optional_count(getattr(usage, "total_tokens", None)),
                raw_response=response,
                collector_terminal_locator=getattr(
                    audit_attempt, "terminal_artifact_locator", None
                ),
            )
        except Exception:
            if logical_call is not None and bool(
                getattr(logical_call, "requires_strict_provider_audit", False)
            ):
                raise
            # Derived audit metadata must never alter provider retry behavior.
            return

    def _finalize_prompt_sentinel_actor_output(
        self,
        logical_call: Any,
        *,
        prediction: str,
        action: JSONAction,
        parser_id: str,
        parser_succeeded: bool,
        parser_attempt_count: int,
        parser_ns: int,
    ) -> None:
        """Complete an optional R2.4 detail without affecting the actor result."""

        if logical_call is None:
            return
        try:
            from mobile_world.runtime.sentinel.r2_4.audit_detail import (
                ParserResultStatusV1,
            )

            action_projection = action.model_dump(mode="json", exclude_none=False)
            logical_call.finalize_actor_output(
                raw_provider_response=prediction,
                raw_parser_input=prediction,
                parsed_action=action_projection,
                parser_id=parser_id,
                parser_status=(
                    ParserResultStatusV1.PARSED
                    if parser_succeeded
                    else ParserResultStatusV1.PARSE_FALLBACK
                ),
                parser_attempt_count=parser_attempt_count,
                parser_ns=max(0, parser_ns),
            )
            if bool(getattr(logical_call, "requires_strict_provider_audit", False)):
                self._last_prompt_sentinel_logical_call = logical_call
        except Exception:
            if bool(getattr(logical_call, "requires_strict_provider_audit", False)):
                raise
            # A derived sidecar failure cannot replace a parsed actor action.
            logger.warning("R2.4 runtime audit finalization failed closed to no detail")

    def finalize_prompt_sentinel_action_execution(
        self,
        *,
        action: JSONAction,
        action_executed: bool,
        action_execution_ns: int = 0,
    ) -> Any:
        """Commit a strict production audit after the driver execution decision."""

        logical_call = self._last_prompt_sentinel_logical_call
        if logical_call is None:
            return None
        projection = action.model_dump(mode="json", exclude_none=False)
        receipt = logical_call.finalize_action_execution(
            parsed_action=projection,
            action_executed=action_executed,
            action_execution_ns=action_execution_ns,
        )
        self._last_prompt_sentinel_logical_call = None
        return receipt

    @property
    def pending_prompt_sentinel_audit_logical_call_id(self) -> str | None:
        logical_call = self._last_prompt_sentinel_logical_call
        if logical_call is None:
            return None
        context = getattr(logical_call, "context", None)
        value = getattr(context, "logical_call_id", None)
        return value if type(value) is str else None

    def _cancel_prompt_sentinel_runtime_audit(self, logical_call: Any) -> None:
        """Release an optional pending detail when no actor result exists."""

        if logical_call is None:
            return
        try:
            logical_call.cancel_runtime_audit()
            if self._last_prompt_sentinel_logical_call is logical_call:
                self._last_prompt_sentinel_logical_call = None
        except Exception:
            if bool(getattr(logical_call, "requires_strict_provider_audit", False)):
                raise
            return

    def _finalize_prompt_sentinel_actor_failure(
        self,
        logical_call: Any,
        *,
        failure_phase: str,
        failure_code: str,
    ) -> Any:
        """Persist strict production failures; optional CPU audits may still cancel."""

        if logical_call is None:
            return None
        if not bool(getattr(logical_call, "requires_strict_provider_audit", False)):
            self._cancel_prompt_sentinel_runtime_audit(logical_call)
            return None
        receipt = logical_call.finalize_actor_failure(
            failure_phase=failure_phase,
            failure_code=failure_code,
        )
        if self._last_prompt_sentinel_logical_call is logical_call:
            self._last_prompt_sentinel_logical_call = None
        return receipt

    def _handle_openai_error(self, error: Exception, kwargs: dict[str, Any]) -> bool:
        """Preserve the original pre-invocation error logging behavior."""

        error_msg = str(error)
        if self._production_safe_logging_active():
            logger.warning("Production actor request preparation failed")
        else:
            logger.warning(f"Error calling OpenAI API: {error}")
        if "max_tokens" in error_msg and "max_completion_tokens" in error_msg:
            if "max_tokens" in kwargs:
                logger.info("Retrying with max_completion_tokens instead of max_tokens")
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                return True
        return False

    def _begin_actor_model_audit_call(self, *, call_role: str = "actor") -> Any:
        """Lazily enter audit capture only when an enabled task context exists."""

        try:
            from mobile_world.runtime.audit.context import get_audit_context

            context = get_audit_context()
            if context is None or not getattr(context.recorder, "enabled", False):
                return None

            from mobile_world.runtime.audit.model_io import begin_model_call

            return begin_model_call(
                call_role=call_role,
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
        if self._production_safe_logging_active():
            logger.debug("Initialized production MCP actor instruction")
        else:
            logger.debug(f"initialized the agent with the given instruction: {self.instruction}")
        return True

    def reset_tools(self, tools: list[dict]) -> None:
        """Reset the tools for the agent."""
        self.tools = tools

    @abstractmethod
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        """Generate the next action based on current observation."""
        raise NotImplementedError("predict method is not implemented")
