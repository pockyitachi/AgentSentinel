from __future__ import annotations

import base64
import copy
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from mobile_world.agents import base as base_module
from mobile_world.agents.base import BaseAgent
from mobile_world.runtime.audit import model_io
from mobile_world.runtime.audit.context import (
    AuditContext,
    ModelCallTrace,
    bind_audit_context,
)
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.null_recorder import NULL_TASK_RECORDER
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.audit.serializer import ArtifactSerializer

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _Agent(BaseAgent):
    def predict(self, observation: dict[str, Any]) -> tuple[str, Any]:
        raise NotImplementedError


class _Response:
    def __init__(
        self,
        content: str = "  final answer  ",
        *,
        reasoning_content: str | None = None,
        response_id: str = "response-1",
    ) -> None:
        self.id = response_id
        self.usage = SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        )
        self.choices = [
            SimpleNamespace(
                index=0,
                finish_reason="stop",
                message=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning_content,
                    tool_calls=[],
                ),
            )
        ]

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
        assert mode == "json"
        assert exclude_none is False
        return {
            "id": self.id,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "content": self.choices[0].message.content,
                        "reasoning_content": self.choices[0].message.reasoning_content,
                        "tool_calls": [],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 3},
                "provider_extension": {"kept": True},
            },
            "provider_extension": "raw-value",
        }


class _ResponseWithBrokenUsage:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.choices = [
            SimpleNamespace(
                index=0,
                finish_reason="stop",
                message=SimpleNamespace(
                    content="not returned",
                    reasoning_content=None,
                    tool_calls=[],
                ),
            )
        ]

    @property
    def usage(self) -> Any:
        raise self._error

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
        assert mode == "json"
        assert exclude_none is False
        return {
            "id": "response-with-broken-usage",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "content": "not returned",
                        "reasoning_content": None,
                        "tool_calls": [],
                    },
                }
            ],
            "usage": {"provider_usage_parse_error": True},
        }


class _Chunk:
    def __init__(
        self,
        *,
        content: str | None = None,
        reasoning: str | None = None,
        finish_reason: str | None = None,
        usage: bool = False,
        response_id: str = "stream-1",
    ) -> None:
        self.id = response_id
        self.usage = (
            SimpleNamespace(
                prompt_tokens=13,
                completion_tokens=5,
                prompt_tokens_details=SimpleNamespace(cached_tokens=2),
            )
            if usage
            else None
        )
        self.choices = (
            [
                SimpleNamespace(
                    index=0,
                    finish_reason=finish_reason,
                    delta=SimpleNamespace(
                        content=content,
                        reasoning_content=reasoning,
                        tool_calls=None,
                    ),
                )
            ]
            if content is not None or reasoning is not None or finish_reason is not None
            else []
        )

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
        assert mode == "json"
        assert exclude_none is False
        return {
            "id": self.id,
            "choices": [
                {
                    "index": choice.index,
                    "finish_reason": choice.finish_reason,
                    "delta": {
                        "content": choice.delta.content,
                        "reasoning_content": choice.delta.reasoning_content,
                        "tool_calls": choice.delta.tool_calls,
                    },
                }
                for choice in self.choices
            ],
            "usage": (
                {
                    "prompt_tokens": self.usage.prompt_tokens,
                    "completion_tokens": self.usage.completion_tokens,
                    "prompt_tokens_details": {
                        "cached_tokens": self.usage.prompt_tokens_details.cached_tokens
                    },
                }
                if self.usage is not None
                else None
            ),
        }


class _ControlledStream(Iterator[_Chunk]):
    def __init__(self, chunks: list[_Chunk], *, error: Exception | None = None) -> None:
        self._chunks = iter(chunks)
        self._error = error
        self._raised = False
        self.started = False
        self.next_calls = 0

    def __iter__(self) -> _ControlledStream:
        return self

    def __next__(self) -> _Chunk:
        self.started = True
        self.next_calls += 1
        try:
            return next(self._chunks)
        except StopIteration:
            if self._error is not None and not self._raised:
                self._raised = True
                raise self._error
            raise


class _Completions:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Client:
    def __init__(self, outcomes: list[Any], *, api_key: str = "test-secret") -> None:
        self.api_key = api_key
        self.base_url = "https://user:password@models.example/v1/?signed=secret"
        self.max_retries = 2
        self.timeout = 120.0
        self.chat = SimpleNamespace(completions=_Completions(outcomes))


def _agent_with(outcomes: list[Any], *, api_key: str = "test-secret") -> _Agent:
    agent = _Agent()
    agent.openai_client = _Client(outcomes, api_key=api_key)
    return agent


def _open_audit_task(
    tmp_path: Any,
    *,
    store_stream_chunks: bool = True,
    known_secrets: tuple[str, ...] = (),
) -> tuple[RunRecorder, TaskRecorder, AuditContext]:
    run = RunRecorder(
        tmp_path,
        producer=Producer.local(version="test", worker_id="pytest-worker"),
        sync=False,
    )
    run.write_manifest_start({})
    task = run.open_task()
    step_id = new_ulid()
    parent = task.append_event(
        "step_started",
        {"step_id": step_id, "observation": None, "agent_observation_keys": []},
    )
    context = AuditContext(
        run_id=run.run_id,
        recorder=task,
        task_run_id=task.task_run_id,
        step_id=step_id,
        model_call_trace=ModelCallTrace(),
        parent_event_id=parent["event_id"],
        store_stream_chunks=store_stream_chunks,
        known_secrets=known_secrets,
    )
    return run, task, context


def _events(task: TaskRecorder, event_type: str | None = None) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in task.path.read_text().splitlines()]
    if event_type is None:
        return events
    return [event for event in events if event["event_type"] == event_type]


def _image_messages() -> list[dict[str, Any]]:
    data_url = f"data:image/png;base64,{base64.b64encode(_ONE_PIXEL_PNG).decode('ascii')}"
    return [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]


def _image_messages_with_empty_marker() -> list[dict[str, Any]]:
    # Align the existing PNG to a base64 block, then append harmless trailing
    # bytes whose canonical base64 contains the local API-key marker.  Pillow
    # accepts trailing PNG bytes, matching the long data-URL shape that exposed
    # the production false positive.
    png_with_trailing_bytes = _ONE_PIXEL_PNG + b"\0" + base64.b64decode("EMPTYAAA", validate=True)
    data_url = f"data:image/png;base64,{base64.b64encode(png_with_trailing_bytes).decode('ascii')}"
    assert "EMPTY" in data_url
    return [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]


def test_feature_off_nonstream_is_exact_and_does_not_serialize(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _Response()
    agent = _agent_with([response])
    messages = _image_messages()
    before = copy.deepcopy(messages)

    monkeypatch.setattr(
        model_io,
        "begin_model_call",
        lambda **_: pytest.fail("disabled audit entered model capture"),
    )
    context = AuditContext(run_id="disabled", recorder=NULL_TASK_RECORDER)
    with bind_audit_context(context):
        result = agent.openai_chat_completions_create(
            model="ordinary-model",
            messages=messages,
            temperature=0.2,
        )

    assert result == "final answer"
    assert messages == before
    assert agent.openai_client.chat.completions.calls[0]["messages"] is messages
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("model", "kwargs", "expected_present", "expected_absent"),
    [
        (
            "claude-test",
            {"temperature": 0.2, "max_tokens": 10},
            {"max_tokens": 64000},
            {"temperature", "max_completion_tokens"},
        ),
        (
            "o1-test",
            {"max_tokens": 10},
            {"max_completion_tokens": 10},
            {"max_tokens"},
        ),
    ],
)
def test_feature_off_keeps_model_specific_sdk_argument_behavior(
    model: str,
    kwargs: dict[str, Any],
    expected_present: dict[str, Any],
    expected_absent: set[str],
) -> None:
    agent = _agent_with([_Response()])
    context = AuditContext(run_id="disabled", recorder=NULL_TASK_RECORDER)
    with bind_audit_context(context):
        assert (
            agent.openai_chat_completions_create(model=model, messages=[], **kwargs)
            == "final answer"
        )

    provider_payload = agent.openai_client.chat.completions.calls[0]
    for key, value in expected_present.items():
        assert provider_payload[key] == value
    assert expected_absent.isdisjoint(provider_payload)


def test_feature_off_keeps_kimi_reasoning_return_and_extra_body() -> None:
    agent = _agent_with([_Response(content=" answer ", reasoning_content=" reason ")])
    context = AuditContext(run_id="disabled", recorder=NULL_TASK_RECORDER)
    with bind_audit_context(context):
        result = agent.openai_chat_completions_create(
            model="kimi-k2.5",
            messages=[],
        )

    assert result == "<think>reason</think>\nanswer"
    assert agent.openai_client.chat.completions.calls[0]["extra_body"] == {"enable_thinking": True}


def test_feature_off_stream_preserves_laziness_chunk_identity_and_exception() -> None:
    first = _Chunk(content="a")
    original_error = RuntimeError("stream broke")
    stream = _ControlledStream([first], error=original_error)
    agent = _agent_with([stream])
    context = AuditContext(run_id="disabled", recorder=NULL_TASK_RECORDER)

    with bind_audit_context(context):
        wrapped = agent.openai_chat_completions_create(
            model="stream-model",
            messages=[],
            stream=True,
        )
        assert stream.started is False
        assert next(wrapped) is first
        with pytest.raises(RuntimeError) as raised:
            next(wrapped)

    assert raised.value is original_error


def test_nonstream_records_final_sdk_payload_raw_response_and_returned_value(
    tmp_path: Any,
) -> None:
    agent = _agent_with([_Response()])
    messages = _image_messages()
    before = copy.deepcopy(messages)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="gpt-5-test",
                messages=messages,
                max_tokens=99,
                temperature=0.1,
                tools=[{"type": "function", "function": {"name": "tap"}}],
            )

        assert result == "final answer"
        assert messages == before
        provider_payload = agent.openai_client.chat.completions.calls[0]
        assert provider_payload["messages"] is messages
        assert provider_payload["max_completion_tokens"] == 99
        assert "max_tokens" not in provider_payload

        request = _events(task, "model_request")[0]
        response = _events(task, "model_response")[0]
        request_payload = request["payload"]
        assert request["caused_by_event_id"] == context.parent_event_id
        assert response["caused_by_event_id"] == request["event_id"]
        assert request_payload["excluded_transport_fields"] == [
            "api_key",
            "authorization_headers",
            "cookies",
        ]
        assert request_payload["endpoint"] == {
            "origin": "https://models.example",
            "path": "/v1/chat/completions",
            "query_removed": True,
        }
        assert request_payload["sdk"]["client_configuration"] == {
            "max_retries": 2,
            "timeout": {"all_seconds": 120.0},
        }
        assert request_payload["sdk"]["transparent_retry_attempts_observable"] is False
        assert len(request_payload["request_images"]) == 1

        serializer = ArtifactSerializer(run.blob_store)
        reconstructed_request = serializer.rehydrate(request_payload["sdk_arguments_snapshot_blob"])
        assert reconstructed_request == provider_payload
        reconstructed_response = serializer.rehydrate(
            response["payload"]["raw_response"]["snapshot_blob"]
        )
        assert reconstructed_response["provider_extension"] == "raw-value"
        assert serializer.rehydrate(response["payload"]["returned_value_snapshot_blob"]) == result
        assert response["payload"]["normalized_response"]["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "cached_tokens": 3,
            "provider_usage": reconstructed_response["usage"],
        }
        assert context.source_model_call_ids() == (request_payload["model_call_id"],)
        assert context.latest_model_terminal_event_id() == response["event_id"]

        raw_files = "".join(
            path.read_text(errors="ignore") for path in run.run_root.rglob("*") if path.is_file()
        )
        assert "test-secret" not in raw_files
        assert "user:password" not in raw_files
        assert "signed=secret" not in raw_files
    finally:
        run.close()


def test_transport_credentials_are_excluded_without_mutating_provider_kwargs(tmp_path) -> None:
    agent = _agent_with([_Response()])
    headers = {
        "Authorization": "Bearer transport-token",
        "Cookie": "session=transport-cookie",
        "X-Trace": "retain-me",
    }
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="fixture-model",
                messages=[{"role": "user", "content": "hello"}],
                extra_headers=headers,
            )

        assert result == "final answer"
        provider_headers = agent.openai_client.chat.completions.calls[0]["extra_headers"]
        assert provider_headers is headers
        assert provider_headers["Authorization"] == "Bearer transport-token"
        request = _events(task, "model_request")[0]["payload"]
        reconstructed = ArtifactSerializer(run.blob_store).rehydrate(
            request["sdk_arguments_snapshot_blob"]
        )
        assert reconstructed["extra_headers"] == {"X-Trace": "retain-me"}
        assert "extra_headers.Authorization" in request["excluded_transport_fields"]
        assert "extra_headers.Cookie" in request["excluded_transport_fields"]
        persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
        assert b"transport-token" not in persisted
        assert b"transport-cookie" not in persisted
        assert task.capture_complete is True
    finally:
        run.close()


def test_configured_secret_in_model_visible_request_is_not_persisted(tmp_path) -> None:
    secret = "prompt-configured-secret"
    agent = _agent_with([_Response()])
    run, task, context = _open_audit_task(tmp_path, known_secrets=(secret,))
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="fixture-model",
                messages=[{"role": "user", "content": f"do not store {secret}"}],
            )

        assert result == "final answer"
        assert _events(task, "model_request") == []
        assert task.capture_complete is False
        assert "sdk_arguments_snapshot_blob" in task.missing_artifacts
        persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
        assert secret.encode() not in persisted
    finally:
        run.close()


def test_uppercase_local_api_key_marker_inside_image_data_url_is_not_a_secret(
    tmp_path: Any,
) -> None:
    agent = _agent_with([_Response()], api_key="EMPTY")
    messages = _image_messages_with_empty_marker()
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="local-model",
                messages=messages,
            )

        assert result == "final answer"
        request = _events(task, "model_request")[0]["payload"]
        reconstructed = ArtifactSerializer(run.blob_store).rehydrate(
            request["sdk_arguments_snapshot_blob"]
        )
        assert reconstructed == agent.openai_client.chat.completions.calls[0]
        assert request["request_images"][0]["capture_status"] == "captured"
        assert task.capture_complete is True
        assert _events(task, "collector_error") == []
    finally:
        run.close()


def test_real_client_api_key_in_model_visible_request_remains_fail_closed(
    tmp_path: Any,
) -> None:
    secret = "real-client-api-secret"
    agent = _agent_with([_Response()], api_key=secret)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="remote-model",
                messages=[{"role": "user", "content": f"do not persist {secret}"}],
            )

        assert result == "final answer"
        assert _events(task, "model_request") == []
        assert task.capture_complete is False
        assert "sdk_arguments_snapshot_blob" in task.missing_artifacts
        persisted = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
        assert secret.encode() not in persisted
    finally:
        run.close()


def test_remote_request_image_without_bytes_marks_capture_incomplete(tmp_path) -> None:
    agent = _agent_with([_Response()])
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "https://images.example/frame.png?opaque=1"},
                }
            ],
        }
    ]
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="fixture-model",
                messages=messages,
            )

        assert result == "final answer"
        request = _events(task, "model_request")[0]["payload"]
        assert request["request_images"][0]["capture_status"] == (
            "url_preserved_content_unavailable"
        )
        assert task.capture_complete is False
        assert "model_request.request_image_content" in task.missing_artifacts
    finally:
        run.close()


@pytest.mark.parametrize(
    ("model", "kwargs", "reasoning", "expected_return"),
    [
        (
            "claude-test",
            {"temperature": 0.2, "max_tokens": 10},
            None,
            "final answer",
        ),
        (
            "kimi-k2.5",
            {"max_tokens": 10},
            " chain of thought ",
            "<think>chain of thought</think>\nfinal answer",
        ),
    ],
)
def test_active_capture_stores_claude_and_kimi_final_mutated_sdk_arguments(
    tmp_path: Any,
    model: str,
    kwargs: dict[str, Any],
    reasoning: str | None,
    expected_return: str,
) -> None:
    agent = _agent_with([_Response(reasoning_content=reasoning)])
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model=model,
                messages=[{"role": "user", "content": "go"}],
                **kwargs,
            )

        assert result == expected_return
        provider_payload = agent.openai_client.chat.completions.calls[0]
        request_payload = _events(task, "model_request")[0]["payload"]
        assert (
            ArtifactSerializer(run.blob_store).rehydrate(
                request_payload["sdk_arguments_snapshot_blob"]
            )
            == provider_payload
        )
        if "claude" in model:
            assert provider_payload["max_tokens"] == 64000
            assert "temperature" not in provider_payload
        else:
            assert provider_payload["extra_body"] == {"enable_thinking": True}
    finally:
        run.close()


def test_nonstream_visible_retry_has_distinct_requests_and_causal_chain(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_error = TimeoutError("provider timeout")
    agent = _agent_with([first_error, _Response(content="ok")])
    sleeps: list[float] = []
    monkeypatch.setattr(base_module.time, "sleep", sleeps.append)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=[{"role": "user", "content": "go"}],
                retry_times=2,
            )

        assert result == "ok"
        assert sleeps == [1]
        requests = _events(task, "model_request")
        failures = _events(task, "model_attempt_failed")
        responses = _events(task, "model_response")
        assert [event["payload"]["attempt_index"] for event in requests] == [1, 2]
        assert len({event["payload"]["request_id"] for event in requests}) == 2
        assert len({event["payload"]["model_call_id"] for event in requests}) == 1
        assert failures[0]["payload"]["exception"]["message"] == "provider timeout"
        assert failures[0]["payload"]["retry_planned"] is True
        assert failures[0]["caused_by_event_id"] == requests[0]["event_id"]
        assert requests[1]["caused_by_event_id"] == failures[0]["event_id"]
        assert responses[0]["caused_by_event_id"] == requests[1]["event_id"]
    finally:
        run.close()


def test_max_tokens_parameter_retry_is_visible_and_does_not_sleep(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = ValueError("max_tokens is unsupported; use max_completion_tokens")
    agent = _agent_with([error, _Response(content="converted")])
    sleeps: list[float] = []
    monkeypatch.setattr(base_module.time, "sleep", sleeps.append)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=[],
                retry_times=1,
                max_tokens=20,
            )

        assert result == "converted"
        assert sleeps == []
        calls = agent.openai_client.chat.completions.calls
        assert calls[0]["max_tokens"] == 20
        assert calls[1]["max_completion_tokens"] == 20
        failures = _events(task, "model_attempt_failed")
        assert failures[0]["payload"]["retry_planned"] is True
        assert [event["payload"]["attempt_index"] for event in _events(task, "model_request")] == [
            1,
            2,
        ]
    finally:
        run.close()


def test_all_nonstream_failures_remain_swallowed_after_visible_attempts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent_with([RuntimeError("one"), RuntimeError("two")])
    sleeps: list[float] = []
    monkeypatch.setattr(base_module.time, "sleep", sleeps.append)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=[],
                retry_times=2,
            )

        assert result is None
        assert sleeps == [1, 1]
        failures = _events(task, "model_attempt_failed")
        assert [event["payload"]["retry_planned"] for event in failures] == [True, False]
        assert len(_events(task, "model_response")) == 0
    finally:
        run.close()


def test_context_known_secret_is_merged_with_client_secret_and_scrubbed(
    tmp_path: Any,
) -> None:
    executor_secret = "executor-environment-secret"
    agent = _agent_with([RuntimeError(f"provider exposed {executor_secret} and test-secret")])
    run, task, context = _open_audit_task(
        tmp_path,
        known_secrets=(executor_secret, "test-secret", executor_secret),
    )
    try:
        with bind_audit_context(context):
            assert (
                agent.openai_chat_completions_create(
                    model="ordinary-model",
                    messages=[],
                    retry_times=1,
                )
                is None
            )

        failure = _events(task, "model_attempt_failed")[0]
        assert failure["payload"]["exception"]["message"] == (
            "provider exposed [REDACTED] and [REDACTED]"
        )
        assert executor_secret not in task.path.read_text(encoding="utf-8")
        assert "test-secret" not in task.path.read_text(encoding="utf-8")
    finally:
        run.close()


def test_provider_returned_response_processing_error_is_visible_and_retried(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    processing_error = RuntimeError("usage parsing failed")
    agent = _agent_with(
        [_ResponseWithBrokenUsage(processing_error), _Response(content="retry succeeded")]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(base_module.time, "sleep", sleeps.append)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=[],
                retry_times=2,
            )

        assert result == "retry succeeded"
        assert sleeps == [1]
        requests = _events(task, "model_request")
        failures = _events(task, "model_attempt_failed")
        assert len(requests) == 2
        assert len(failures) == 1
        failure_payload = failures[0]["payload"]
        assert failure_payload["failure_phase"] == "response_serialization"
        assert failure_payload["retry_planned"] is True
        assert failure_payload["exception"]["message"] == "usage parsing failed"
        raw_response = ArtifactSerializer(run.blob_store).rehydrate(
            failure_payload["raw_response_snapshot_blob"]
        )
        assert raw_response["usage"] == {"provider_usage_parse_error": True}
        assert requests[1]["caused_by_event_id"] == failures[0]["event_id"]
    finally:
        run.close()


def test_stream_is_lazy_yields_same_chunks_and_records_complete_assembly(tmp_path: Any) -> None:
    chunks = [
        _Chunk(reasoning="reason-"),
        _Chunk(reasoning="one", content="hello "),
        _Chunk(),
        _Chunk(content="world", finish_reason="stop", usage=True),
    ]
    stream = _ControlledStream(chunks)
    agent = _agent_with([stream])
    run, task, context = _open_audit_task(tmp_path)
    stream_options: dict[str, Any] = {}
    try:
        with bind_audit_context(context):
            wrapped = agent.openai_chat_completions_create(
                model="stream-model",
                messages=[{"role": "user", "content": "go"}],
                stream=True,
                stream_options=stream_options,
            )
            assert stream.started is False
            first = next(wrapped)
            assert first is chunks[0]
            assert len(_events(task, "model_stream_chunk")) == 1
            observed = [first, *list(wrapped)]

        assert observed == chunks
        assert all(actual is expected for actual, expected in zip(observed, chunks))
        requests = _events(task, "model_request")
        chunk_events = _events(task, "model_stream_chunk")
        responses = _events(task, "model_response")
        assert [event["payload"]["chunk_index"] for event in chunk_events] == [0, 1, 2, 3]
        assert responses[0]["payload"]["stream_state"] == "complete"
        assert responses[0]["payload"]["raw_response"]["chunk_event_ids"] == [
            event["event_id"] for event in chunk_events
        ]
        normalized = responses[0]["payload"]["normalized_response"]
        assert normalized["choices"][0]["reasoning_content"] == "reason-one"
        assert normalized["choices"][0]["content"] == "hello world"
        assert normalized["usage"]["cached_tokens"] == 2
        assert responses[0]["caused_by_event_id"] == chunk_events[-1]["event_id"]
        assert requests[0]["payload"]["request_view"]["stream_options"] == {"include_usage": True}
        assert agent.openai_client.chat.completions.calls[0]["stream_options"] is stream_options
        assert agent.get_total_token_usage() == {
            "completion_tokens": 5,
            "prompt_tokens": 13,
            "cached_tokens": 2,
            "total_tokens": 18,
        }
    finally:
        run.close()


def test_stream_chunk_storage_can_be_disabled_without_changing_live_stream(
    tmp_path: Any,
) -> None:
    chunks = [_Chunk(content="one"), _Chunk(content="two", finish_reason="stop")]
    stream = _ControlledStream(chunks)
    agent = _agent_with([stream])
    run, task, context = _open_audit_task(tmp_path, store_stream_chunks=False)
    try:
        with bind_audit_context(context):
            wrapped = agent.openai_chat_completions_create(
                model="stream-model",
                messages=[],
                stream=True,
            )
            observed = list(wrapped)

        assert all(actual is expected for actual, expected in zip(observed, chunks))
        assert len(_events(task, "model_stream_chunk")) == 0
        response = _events(task, "model_response")[0]["payload"]
        assert response["raw_response"] == {
            "kind": "stream_chunks_omitted_by_config",
            "snapshot_blob": None,
            "chunk_event_ids": [],
            "chunk_count": 0,
            "observed_chunk_count": 2,
        }
        assert response["normalized_response"] is None
        assert task.capture_complete is False
        assert task.missing_artifacts == ("model_stream_chunks",)
    finally:
        run.close()


def test_stream_iteration_error_keeps_partial_chunks_and_same_exception(tmp_path: Any) -> None:
    original_error = RuntimeError("provider stream failed")
    chunks = [_Chunk(content="one"), _Chunk(content="two")]
    agent = _agent_with([_ControlledStream(chunks, error=original_error)])
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            wrapped = agent.openai_chat_completions_create(
                model="stream-model",
                messages=[],
                stream=True,
            )
            assert next(wrapped) is chunks[0]
            assert next(wrapped) is chunks[1]
            with pytest.raises(RuntimeError) as raised:
                next(wrapped)

        assert raised.value is original_error
        chunk_events = _events(task, "model_stream_chunk")
        failures = _events(task, "model_attempt_failed")
        assert len(failures) == 1
        assert failures[0]["payload"]["failure_phase"] == "stream_iteration"
        assert failures[0]["payload"]["partial_chunk_event_ids"] == [
            event["event_id"] for event in chunk_events
        ]
        assert (
            failures[0]["payload"]["normalized_partial_response"]["choices"][0]["content"]
            == "onetwo"
        )
        assert len(_events(task, "model_response")) == 0
    finally:
        run.close()


def test_stream_sdk_create_failure_records_terminal_and_preserves_same_exception(
    tmp_path: Any,
) -> None:
    original_error = ConnectionError("stream create failed")
    agent = _agent_with([original_error])
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context), pytest.raises(ConnectionError) as raised:
            agent.openai_chat_completions_create(
                model="stream-model",
                messages=[],
                stream=True,
            )

        assert raised.value is original_error
        requests = _events(task, "model_request")
        failures = _events(task, "model_attempt_failed")
        assert len(requests) == 1
        assert len(failures) == 1
        assert failures[0]["payload"]["failure_phase"] == "provider_call"
        assert failures[0]["payload"]["retry_planned"] is False
        assert failures[0]["caused_by_event_id"] == requests[0]["event_id"]
        assert len(_events(task, "model_response")) == 0
        assert len(_events(task, "model_stream_chunk")) == 0
        assert context.latest_model_terminal_event_id() == failures[0]["event_id"]
    finally:
        run.close()


def test_consumer_close_records_one_abandoned_terminal_without_read_ahead(tmp_path: Any) -> None:
    chunks = [_Chunk(content="first"), _Chunk(content="never-read")]
    stream = _ControlledStream(chunks)
    agent = _agent_with([stream])
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            wrapped = agent.openai_chat_completions_create(
                model="stream-model",
                messages=[],
                stream=True,
            )
            assert next(wrapped) is chunks[0]
            wrapped.close()

        responses = _events(task, "model_response")
        assert len(responses) == 1
        assert responses[0]["payload"]["stream_state"] == "consumer_abandoned"
        assert responses[0]["payload"]["raw_response"]["observed_chunk_count"] == 1
        assert len(_events(task, "model_stream_chunk")) == 1
        assert stream.started is True
        assert stream.next_calls == 1
    finally:
        run.close()


def test_fail_open_capture_fault_marks_incomplete_without_changing_provider_result(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent_with([_Response(content="still returned")])
    run, task, context = _open_audit_task(tmp_path)

    def fail_snapshot(*_: Any, **__: Any) -> Any:
        raise OSError("audit disk unavailable")

    monkeypatch.setattr(ArtifactSerializer, "snapshot_sdk_arguments", fail_snapshot)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=[{"role": "user", "content": "unchanged"}],
            )

        assert result == "still returned"
        assert len(agent.openai_client.chat.completions.calls) == 1
        assert len(_events(task, "collector_error")) == 1
        assert len(_events(task, "model_request")) == 0
        assert task.capture_complete is False
        assert task.missing_artifacts == ("sdk_arguments_snapshot_blob",)
    finally:
        run.close()


@pytest.mark.parametrize("fault_point", ["request_preprocessing", "request_payload"])
def test_request_preparation_fault_never_blocks_or_changes_provider_call(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    response = _Response(content="provider result")
    agent = _agent_with([response])
    messages = [{"role": "user", "content": "same object"}]
    run, task, context = _open_audit_task(tmp_path)

    def collector_fault(*_: Any, **__: Any) -> Any:
        raise OSError(f"collector {fault_point} failed")

    target = (
        "_sdk_arguments_for_audit" if fault_point == "request_preprocessing" else "_package_version"
    )
    monkeypatch.setattr(model_io, target, collector_fault)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=messages,
                temperature=0.4,
            )

        assert result == "provider result"
        assert len(agent.openai_client.chat.completions.calls) == 1
        provider_call = agent.openai_client.chat.completions.calls[0]
        assert provider_call["messages"] is messages
        assert provider_call["temperature"] == 0.4
        assert _events(task, "model_request") == []
        assert len(_events(task, "collector_error")) == 1
        assert task.capture_complete is False
    finally:
        run.close()


@pytest.mark.parametrize("fault_point", ["begin_call", "begin_attempt", "response_record"])
def test_base_collector_facade_faults_do_not_change_nonstream_result(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    response = _Response(content="unchanged")
    agent = _agent_with([response])
    messages = [{"role": "user", "content": "hello"}]
    run, _, context = _open_audit_task(tmp_path)

    def collector_fault(*_: Any, **__: Any) -> Any:
        raise RuntimeError(f"collector {fault_point} failed")

    if fault_point == "begin_call":
        monkeypatch.setattr(model_io, "begin_model_call", collector_fault)
    elif fault_point == "begin_attempt":
        monkeypatch.setattr(model_io.ModelCallAudit, "begin_attempt", collector_fault)
    else:
        monkeypatch.setattr(
            model_io.ModelAttemptAudit,
            "record_nonstream_response",
            collector_fault,
        )
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=messages,
                temperature=0.3,
            )

        assert result == "unchanged"
        assert len(agent.openai_client.chat.completions.calls) == 1
        provider_call = agent.openai_client.chat.completions.calls[0]
        assert provider_call["messages"] is messages
        assert provider_call["temperature"] == 0.3
    finally:
        run.close()


def test_failure_recorder_fault_keeps_nonstream_retry_sleep_and_result(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_error = TimeoutError("provider timeout")
    agent = _agent_with([provider_error, _Response(content="retry result")])
    messages = [{"role": "user", "content": "retry unchanged"}]
    sleeps: list[float] = []
    monkeypatch.setattr(base_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        model_io.ModelAttemptAudit,
        "record_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("failure recorder broke")),
    )
    run, _, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=messages,
                retry_times=2,
            )

        assert result == "retry result"
        assert sleeps == [1]
        assert len(agent.openai_client.chat.completions.calls) == 2
        assert all(
            call["messages"] is messages for call in agent.openai_client.chat.completions.calls
        )
    finally:
        run.close()


@pytest.mark.parametrize("event_type", ["model_request", "model_response"])
def test_request_or_response_writer_fault_keeps_exact_nonstream_behavior(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    agent = _agent_with([_Response(content="writer-independent")])
    messages = [{"role": "user", "content": "unchanged"}]
    run, task, context = _open_audit_task(tmp_path)
    original_append = task.append_event

    def faulting_append(
        actual_event_type: str,
        payload: Any,
        caused_by_event_id: str | None = None,
    ) -> Any:
        if actual_event_type == event_type:
            raise OSError(f"cannot persist {event_type}")
        return original_append(actual_event_type, payload, caused_by_event_id)

    monkeypatch.setattr(task, "append_event", faulting_append)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=messages,
            )

        assert result == "writer-independent"
        assert len(agent.openai_client.chat.completions.calls) == 1
        assert agent.openai_client.chat.completions.calls[0]["messages"] is messages
        assert len(_events(task, "collector_error")) == 1
        assert task.capture_complete is False
    finally:
        run.close()


@pytest.mark.parametrize("fault_point", ["raw_response", "returned_value", "normalization"])
def test_response_capture_faults_do_not_change_returned_value(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    response = _Response(content="exact return")
    agent = _agent_with([response])
    run, task, context = _open_audit_task(tmp_path)

    if fault_point == "normalization":
        monkeypatch.setattr(
            model_io,
            "normalize_nonstream_response",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("normalize failed")),
        )
    else:
        original_snapshot = ArtifactSerializer.snapshot

        def faulting_snapshot(
            serializer: ArtifactSerializer,
            value: Any,
            **kwargs: Any,
        ) -> Any:
            should_fail = (fault_point == "raw_response" and value is response) or (
                fault_point == "returned_value" and value == "exact return"
            )
            if should_fail:
                raise OSError(f"{fault_point} snapshot failed")
            return original_snapshot(serializer, value, **kwargs)

        monkeypatch.setattr(ArtifactSerializer, "snapshot", faulting_snapshot)
    try:
        with bind_audit_context(context):
            result = agent.openai_chat_completions_create(
                model="ordinary-model",
                messages=[],
            )

        assert result == "exact return"
        assert len(agent.openai_client.chat.completions.calls) == 1
        assert task.capture_complete is False
        assert _events(task, "collector_error")
    finally:
        run.close()


@pytest.mark.parametrize("fault_point", ["chunk_snapshot", "chunk_writer", "terminal_writer"])
def test_stream_capture_faults_preserve_chunk_order_and_identity(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    chunks = [_Chunk(content="one"), _Chunk(content="two", finish_reason="stop")]
    stream = _ControlledStream(chunks)
    agent = _agent_with([stream])
    stream_options: dict[str, Any] = {}
    run, task, context = _open_audit_task(tmp_path)

    if fault_point == "chunk_snapshot":
        original_snapshot = ArtifactSerializer.snapshot

        def faulting_snapshot(
            serializer: ArtifactSerializer,
            value: Any,
            **kwargs: Any,
        ) -> Any:
            if isinstance(value, _Chunk):
                raise OSError("chunk snapshot failed")
            return original_snapshot(serializer, value, **kwargs)

        monkeypatch.setattr(ArtifactSerializer, "snapshot", faulting_snapshot)
    else:
        failed_event = "model_stream_chunk" if fault_point == "chunk_writer" else "model_response"
        original_append = task.append_event

        def faulting_append(
            actual_event_type: str,
            payload: Any,
            caused_by_event_id: str | None = None,
        ) -> Any:
            if actual_event_type == failed_event:
                raise OSError(f"{failed_event} writer failed")
            return original_append(actual_event_type, payload, caused_by_event_id)

        monkeypatch.setattr(task, "append_event", faulting_append)
    try:
        with bind_audit_context(context):
            wrapped = agent.openai_chat_completions_create(
                model="stream-model",
                messages=[],
                stream=True,
                stream_options=stream_options,
            )
            observed = list(wrapped)

        assert len(observed) == len(chunks)
        assert all(actual is expected for actual, expected in zip(observed, chunks))
        assert len(agent.openai_client.chat.completions.calls) == 1
        assert agent.openai_client.chat.completions.calls[0]["stream_options"] is stream_options
        assert task.capture_complete is False
        assert _events(task, "collector_error")
    finally:
        run.close()


def test_stream_failure_capture_fault_preserves_original_provider_exception(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_error = ConnectionError("exact provider failure")
    agent = _agent_with([provider_error])
    run, task, context = _open_audit_task(tmp_path)
    original_append = task.append_event

    def faulting_append(
        event_type: str,
        payload: Any,
        caused_by_event_id: str | None = None,
    ) -> Any:
        if event_type in {"model_attempt_failed", "collector_error"}:
            raise OSError("failure evidence writer unavailable")
        return original_append(event_type, payload, caused_by_event_id)

    monkeypatch.setattr(task, "append_event", faulting_append)
    try:
        with bind_audit_context(context), pytest.raises(ConnectionError) as raised:
            agent.openai_chat_completions_create(
                model="stream-model",
                messages=[],
                stream=True,
            )

        assert raised.value is provider_error
        assert len(agent.openai_client.chat.completions.calls) == 1
        assert task.capture_complete is False
    finally:
        run.close()


def test_stream_public_capture_methods_cannot_replace_iteration_exception(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunk = _Chunk(content="visible")
    provider_error = RuntimeError("exact stream iteration failure")
    agent = _agent_with([_ControlledStream([chunk], error=provider_error)])

    def collector_fault(*_: Any, **__: Any) -> Any:
        raise OSError("collector method failed")

    monkeypatch.setattr(model_io.ModelAttemptAudit, "record_stream_chunk", collector_fault)
    monkeypatch.setattr(model_io.ModelAttemptAudit, "record_failure", collector_fault)
    monkeypatch.setattr(model_io.ModelAttemptAudit, "complete_stream", collector_fault)
    run, _, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            wrapped = agent.openai_chat_completions_create(
                model="stream-model",
                messages=[],
                stream=True,
            )
            assert next(wrapped) is chunk
            with pytest.raises(RuntimeError) as raised:
                next(wrapped)

        assert raised.value is provider_error
        assert len(agent.openai_client.chat.completions.calls) == 1
    finally:
        run.close()
