from __future__ import annotations

import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import requests
from PIL import Image

import mobile_world.runtime.audit.execution_io as execution_io_module
import mobile_world.runtime.client as client_module
from mobile_world.runtime.audit.config import CollectorMode
from mobile_world.runtime.audit.context import AuditContext, bind_audit_context
from mobile_world.runtime.audit.execution_io import (
    ExecutionEvidenceTrace,
    bind_execution_evidence_trace,
)
from mobile_world.runtime.audit.null_recorder import NULL_TASK_RECORDER
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.client import AndroidEnvClient, AndroidMCPEnvClient
from mobile_world.runtime.utils.models import MCP, JSONAction

_SECRET = "configured-secret-value"


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        json_value: Any = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self._json_value = json_value

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if self._json_value is not None:
            return self._json_value
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _StepEnvelopeResponse:
    """Preserve exact fake transport bytes while modeling the server's success envelope."""

    def __init__(self, response: FakeResponse, request_body: dict[str, Any]) -> None:
        self._response = response
        self.content = response.content
        self.status_code = response.status_code
        self.headers = response.headers
        self._request_body = request_body

    @property
    def ok(self) -> bool:
        return self._response.ok

    @property
    def text(self) -> str:
        return self._response.text

    def json(self) -> dict[str, Any]:
        try:
            decoded = self._response.json()
        except Exception:
            decoded = None
        result = decoded.get("result") if type(decoded) is dict else self._response.text
        return {
            "action": self._request_body["action"],
            "device": self._request_body["device"],
            "result": result,
        }

    def raise_for_status(self) -> None:
        self._response.raise_for_status()


class _PatchedRequestsSession:
    """Route the owned-session client through this module's monkeypatched requests calls."""

    def post(self, url: str, **kwargs: Any) -> Any:
        kwargs.pop("timeout", None)
        response = client_module.requests.post(url, **kwargs)
        request_body = kwargs.get("json")
        if url.endswith("/step") and type(request_body) is dict:
            return _StepEnvelopeResponse(response, request_body)
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        kwargs.pop("timeout", None)
        return client_module.requests.get(url, **kwargs)


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _client() -> AndroidEnvClient:
    client = object.__new__(AndroidEnvClient)
    client.base_url = "http://fixture.invalid"
    client.device = "fixture-device"
    client.step_wait_time = 0
    client._initialized = True
    client._request_deadline_monotonic_ns = None
    client._session = _PatchedRequestsSession()
    return client


def _make_trace(
    root: Path,
    *,
    mode: CollectorMode = CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER,
    secrets: tuple[str, ...] = (_SECRET,),
) -> tuple[RunRecorder, TaskRecorder, AuditContext, ExecutionEvidenceTrace]:
    recorder = RunRecorder(
        root,
        producer=Producer.local(version="test", worker_id="execution-io-test"),
        collector_mode=mode,
        sync=False,
    )
    recorder.write_manifest_start({"run_id": recorder.run_id})
    task = recorder.open_task()
    context = AuditContext(
        run_id=recorder.run_id,
        task_run_id=task.task_run_id,
        step_id="01K2Y000000000000000000001",
        recorder=task,
        known_secrets=secrets,
    )
    trace = ExecutionEvidenceTrace.from_context(context)
    assert trace is not None
    return recorder, task, context.derive(execution_evidence_trace=trace), trace


def _events(task: TaskRecorder) -> list[dict[str, Any]]:
    return [json.loads(line) for line in task.path.read_text(encoding="utf-8").splitlines()]


def test_gui_transport_captures_existing_calls_exact_bytes_and_source_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder, task, context, trace = _make_trace(tmp_path)
    screenshot_bytes = _png_bytes((10, 20, 30))
    response_bytes = b"\x00exact-step-response\xff"
    response = FakeResponse(
        response_bytes,
        status_code=202,
        headers={
            "Content-Type": "application/octet-stream",
            "Set-Cookie": f"session={_SECRET}",
            "Authorization": f"Bearer {_SECRET}",
            "X-Debug": f"prefix-{_SECRET}-suffix",
            "Location": (
                "https://user:password@example.test/object"
                "?X-Amz-Signature=signed-value&X-Amz-Expires=60"
            ),
        },
    )
    screenshot_response = FakeResponse(
        b"unused",
        json_value={"b64_png": base64.b64encode(screenshot_bytes).decode("ascii")},
    )
    posts: list[tuple[str, dict[str, Any]]] = []
    gets: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, *, json: dict[str, Any]) -> FakeResponse:
        posts.append((url, json))
        return response

    def fake_get(url: str, *, params: dict[str, Any]) -> FakeResponse:
        gets.append((url, params))
        return screenshot_response

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.requests, "get", fake_get)
    client = _client()
    action = JSONAction(action_type="click", x=17, y=29)
    action_before = action.model_dump()

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="gui")
            observation = client.execute_action(action)
            evidence = trace.finish_execution(observation=observation)

        assert action.model_dump() == action_before
        assert len(posts) == 1
        assert len(gets) == 1
        request_mapping = posts[0][1]
        assert request_mapping == {
            "device": "fixture-device",
            "action": action_before,
        }

        result = evidence.execution_result
        assert result is not None
        assert result["request_endpoint"] == "http://fixture.invalid/step"
        request_ref = result["request_body_snapshot_blob"]
        assert trace.serializer.rehydrate(request_ref) == request_mapping
        assert recorder.blob_store.read_bytes(result["response_body_blob"]) == response_bytes
        assert result["http_status"] == 202
        assert result["response_headers"] == {
            "Content-Type": "application/octet-stream",
            "X-Debug": "prefix-[REDACTED]-suffix",
            "Location": "https://example.test/object",
        }
        assert result["excluded_transport_fields"] == ["Set-Cookie", "Authorization"]
        assert _SECRET not in json.dumps(result)
        assert evidence.duration_ns >= 0

        assert trace.source_screenshot_bytes(observation.screenshot) == screenshot_bytes
        with Image.open(io.BytesIO(screenshot_bytes)) as expected:
            assert observation.screenshot.tobytes() == expected.tobytes()
        assert task.capture_complete is True
    finally:
        recorder.close()


def test_gui_endpoint_removes_userinfo_query_and_fragment_without_losing_body(
    tmp_path: Path,
) -> None:
    recorder, task, context, trace = _make_trace(tmp_path)
    request_body = {"device": "fixture-device", "action": {"action_type": "click"}}
    endpoint = (
        f"https://user:{_SECRET}@[2001:db8::1]:8443/step?api_key={_SECRET}&keep=discarded#fragment"
    )

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="gui")
            trace.record_gui_request(request_body, request_endpoint=endpoint)
            evidence = trace.fail_execution(RuntimeError("fixture transport failure"))

        result = evidence.execution_result
        assert result is not None
        assert result["request_endpoint"] == "https://[2001:db8::1]:8443/step"
        assert trace.serializer.rehydrate(result["request_body_snapshot_blob"]) == request_body
        assert _SECRET not in json.dumps(result)
        assert task.capture_complete is True
    finally:
        recorder.close()


def test_invalid_gui_endpoint_is_explicitly_incomplete_but_body_is_still_captured(
    tmp_path: Path,
) -> None:
    recorder, task, context, trace = _make_trace(tmp_path)
    request_body = {"device": "fixture-device", "action": {"action_type": "click"}}

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="gui")
            trace.record_gui_request(request_body, request_endpoint="relative/step")
            evidence = trace.fail_execution(RuntimeError("fixture transport failure"))

        result = evidence.execution_result
        assert result is not None
        assert result["request_endpoint"] is None
        assert trace.serializer.rehydrate(result["request_body_snapshot_blob"]) == request_body
        assert task.capture_complete is False
        assert "request_endpoint" in task.missing_artifacts
        errors = [event for event in _events(task) if event["event_type"] == "collector_error"]
        assert len(errors) == 1
        assert errors[0]["payload"]["scope"] == "execution_evidence.gui_request_endpoint"
    finally:
        recorder.close()


def test_ask_user_response_is_exact_and_non_200_failure_keeps_partial_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder, _, context, trace = _make_trace(tmp_path)
    screenshot_bytes = _png_bytes((4, 5, 6))
    responses = [
        FakeResponse(
            b'{"result":"  exact user reply\\n"}',
            headers={"Content-Type": "application/json"},
        ),
        FakeResponse(
            b'{"error":"still exact"}',
            status_code=503,
            headers={"Content-Type": "application/json"},
        ),
    ]
    screenshot_response = FakeResponse(
        b"unused",
        json_value={"b64_png": base64.b64encode(screenshot_bytes).decode("ascii")},
    )
    post_count = 0

    def fake_post(*_: Any, **__: Any) -> FakeResponse:
        nonlocal post_count
        response = responses[post_count]
        post_count += 1
        return response

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(
        client_module.requests, "get", lambda *_args, **_kwargs: screenshot_response
    )
    client = _client()
    action = JSONAction(action_type="ask_user", text="question")

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="ask_user")
            observation = client.execute_action(action)
            success = trace.finish_execution(observation=observation)
            assert observation.ask_user_response == "  exact user reply\n"
            assert success.execution_result is not None
            assert success.execution_result["ask_user_response"] == "  exact user reply\n"
            assert (
                recorder.blob_store.read_bytes(success.execution_result["response_body_blob"])
                == responses[0].content
            )

            trace.begin_execution(execution_kind="ask_user")
            with pytest.raises(RuntimeError) as caught:
                client.execute_action(action)
            failed = trace.fail_execution(caught.value)

        assert failed.execution_result is not None
        assert failed.execution_result["http_status"] == 503
        assert (
            recorder.blob_store.read_bytes(failed.execution_result["response_body_blob"])
            == responses[1].content
        )
        assert failed.execution_result["exception"]["message"] == str(caught.value)
        # The failing ask-user path returns before the screenshot call.
        assert post_count == 2
    finally:
        recorder.close()


def test_mcp_raw_result_is_saved_before_in_place_conversion_and_visible_result_after(
    tmp_path: Path,
) -> None:
    recorder, _, context, trace = _make_trace(tmp_path)
    raw_html = "<!DOCTYPE html><html><body><h1>Exact raw</h1></body></html>"
    raw_result = {"text": raw_html, "nested": {"value": 7}}

    class FakeMCP:
        call_count = 0
        received_arguments: Any = None

        def call_tool_sync(self, name: Any, arguments: Any) -> Any:
            assert name == "fixture_tool"
            self.call_count += 1
            self.received_arguments = arguments
            arguments["query"] = "mutated by existing MCP client"
            return raw_result

    client = object.__new__(AndroidMCPEnvClient)
    client.tool_map = {"fixture_tool": FakeMCP()}
    screenshot = Image.new("RGB", (2, 2), (1, 2, 3))
    client.get_screenshot = lambda *, wait_to_stabilize: screenshot
    action = JSONAction(
        action_type=MCP,
        action_name="fixture_tool",
        action_json={"query": "exact arguments", "limit": 3},
    )
    live_arguments = action.action_json

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="mcp")
            observation = client.execute_action(action)
            evidence = trace.finish_execution(observation=observation)

        result = evidence.execution_result
        assert result is not None
        assert result["kind"] == "mcp_tool"
        assert trace.serializer.rehydrate(result["request_body_snapshot_blob"]) == {
            "action_name": "fixture_tool",
            "action_json": {"query": "exact arguments", "limit": 3},
        }
        assert trace.serializer.rehydrate(result["raw_tool_result_blob"]) == {
            "text": raw_html,
            "nested": {"value": 7},
        }
        assert result["agent_visible_tool_result"] == observation.tool_call
        assert (
            trace.serializer.rehydrate(result["agent_visible_tool_result_snapshot_blob"])
            == observation.tool_call
        )
        assert observation.tool_call["text"] != raw_html
        assert raw_result is not trace.serializer.rehydrate(result["raw_tool_result_blob"])
        assert client.tool_map["fixture_tool"].call_count == 1
        assert client.tool_map["fixture_tool"].received_arguments is live_arguments
        assert action.action_json == {
            "query": "mutated by existing MCP client",
            "limit": 3,
        }
    finally:
        recorder.close()


def test_mcp_call_exception_preserves_request_snapshot_and_exception_identity(
    tmp_path: Path,
) -> None:
    recorder, task, context, trace = _make_trace(tmp_path)
    original = RuntimeError("exact MCP call failure")
    calls = 0

    class FailingMCP:
        def call_tool_sync(self, name: Any, arguments: Any) -> Any:
            nonlocal calls
            calls += 1
            assert name == "fixture_tool"
            assert arguments is live_arguments
            raise original

    client = object.__new__(AndroidMCPEnvClient)
    client.tool_map = {"fixture_tool": FailingMCP()}
    client.get_screenshot = lambda **_: pytest.fail("screenshot must not run after MCP failure")
    action = JSONAction(
        action_type=MCP,
        action_name="fixture_tool",
        action_json={"query": "exact arguments"},
    )
    live_arguments = action.action_json

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="mcp")
            with pytest.raises(RuntimeError) as caught:
                client.execute_action(action)
            evidence = trace.fail_execution(caught.value)

        assert caught.value is original
        assert calls == 1
        result = evidence.execution_result
        assert result is not None
        assert trace.serializer.rehydrate(result["request_body_snapshot_blob"]) == {
            "action_name": "fixture_tool",
            "action_json": {"query": "exact arguments"},
        }
        assert result["raw_tool_result_blob"] is None
        assert result["agent_visible_tool_result"] is None
        assert result["exception"]["message"] == str(original)
        assert task.capture_complete is True
    finally:
        recorder.close()


def test_mcp_screenshot_failure_retains_raw_and_post_transform_results(
    tmp_path: Path,
) -> None:
    recorder, task, context, trace = _make_trace(tmp_path)
    raw_html = "<!DOCTYPE html><html><body><h1>Exact raw</h1></body></html>"
    raw_result = {"text": raw_html, "nested": {"value": 9}}
    screenshot_error = RuntimeError("exact screenshot failure")
    screenshot_calls = 0

    class FakeMCP:
        def call_tool_sync(self, _name: Any, _arguments: Any) -> Any:
            return raw_result

    def fail_screenshot(*, wait_to_stabilize: bool) -> Image.Image:
        nonlocal screenshot_calls
        screenshot_calls += 1
        assert wait_to_stabilize is True
        raise screenshot_error

    client = object.__new__(AndroidMCPEnvClient)
    client.tool_map = {"fixture_tool": FakeMCP()}
    client.get_screenshot = fail_screenshot
    action = JSONAction(
        action_type=MCP,
        action_name="fixture_tool",
        action_json={"query": "exact arguments"},
    )

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="mcp")
            with pytest.raises(RuntimeError) as caught:
                client.execute_action(action)
            evidence = trace.fail_execution(caught.value)

        assert caught.value is screenshot_error
        assert screenshot_calls == 1
        result = evidence.execution_result
        assert result is not None
        assert trace.serializer.rehydrate(result["raw_tool_result_blob"]) == {
            "text": raw_html,
            "nested": {"value": 9},
        }
        assert result["agent_visible_tool_result"] is raw_result
        assert raw_result["text"] != raw_html
        assert (
            trace.serializer.rehydrate(result["agent_visible_tool_result_snapshot_blob"])
            == raw_result
        )
        assert result["exception"]["message"] == str(screenshot_error)
        assert task.capture_complete is True
    finally:
        recorder.close()


def test_large_non_json_agent_visible_tool_result_has_authoritative_round_trip(
    tmp_path: Path,
) -> None:
    recorder, _, context, trace = _make_trace(tmp_path)
    exact_binary = bytes(range(256)) * 300
    raw_result = {
        "payload": exact_binary,
        "tuple_value": ("one", 2, None),
    }

    class FakeMCP:
        def call_tool_sync(self, _name: Any, _arguments: Any) -> Any:
            return raw_result

    client = object.__new__(AndroidMCPEnvClient)
    client.tool_map = {"binary_tool": FakeMCP()}
    client.get_screenshot = lambda *, wait_to_stabilize: Image.new("RGB", (1, 1))
    action = JSONAction(
        action_type=MCP,
        action_name="binary_tool",
        action_json={"exact": True},
    )

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="mcp")
            observation = client.execute_action(action)
            evidence = trace.finish_execution(observation=observation)

        result = evidence.execution_result
        assert result is not None
        assert result["agent_visible_tool_result"] is observation.tool_call
        assert observation.tool_call["payload"] == exact_binary

        visible_round_trip = trace.serializer.rehydrate(
            result["agent_visible_tool_result_snapshot_blob"]
        )
        encoded = visible_round_trip["payload"]["$typed_value"]
        assert encoded["kind"] == "bytes"
        assert base64.b64decode(encoded["base64"]) == exact_binary
        assert visible_round_trip["tuple_value"] == {
            "$typed_value": {
                "kind": "tuple",
                "items": ["one", 2, None],
            }
        }

        raw_round_trip = trace.serializer.rehydrate(result["raw_tool_result_blob"])
        assert base64.b64decode(raw_round_trip["payload"]["$typed_value"]["base64"]) == exact_binary

        capture = RunnerTaskCapture(trace.recorder, configured_secrets=(_SECRET,))
        capture.start_task(
            task_name="FixtureTask",
            task_goal="fixture",
            task_goal_status="resolved",
            task_index=1,
            suite_family="mobile_world",
            agent={"adapter": "fixture", "model": "none", "configuration": {}},
            environment={"backend_id": "fixture", "device_id": "fixture"},
            whole_task_attempt_index=1,
        )
        capture.start_step(step_index=1, observation={"screenshot": Image.new("RGB", (1, 1))})
        decision = capture.record_decision(prediction="exact", action=action)
        execution = capture.execution_started(decision=decision, execution_kind="mcp")
        capture.transition_completed(
            post_observation=observation,
            execution=execution,
            execution_result=result,
            duration_ns=evidence.duration_ns,
        )

        transition = next(
            event
            for event in _events(trace.recorder)
            if event["event_type"] == "transition_completed"
        )
        persisted = transition["payload"]["execution_result"]
        assert (
            persisted["agent_visible_tool_result_snapshot_blob"]
            == result["agent_visible_tool_result_snapshot_blob"]
        )
        placeholder = persisted["agent_visible_tool_result"]["$artifact_snapshot"]
        persisted_round_trip = trace.serializer.rehydrate(placeholder["snapshot_blob"])
        assert (
            base64.b64decode(persisted_round_trip["payload"]["$typed_value"]["base64"])
            == exact_binary
        )
    finally:
        recorder.close()


def test_environment_exception_identity_is_preserved_and_audit_view_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder, _, context, trace = _make_trace(tmp_path)
    original = requests.ConnectionError(
        f"failed {_SECRET} at https://example.test/x?X-Amz-Signature=abc&expires=5"
    )
    calls = 0

    def fail_post(*_: Any, **__: Any) -> Any:
        nonlocal calls
        calls += 1
        raise original

    monkeypatch.setattr(client_module.requests, "post", fail_post)
    client = _client()
    action = JSONAction(action_type="click", x=1, y=2)

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="gui")
            with pytest.raises(requests.ConnectionError) as caught:
                client.execute_action(action)
            evidence = trace.fail_execution(caught.value)

        assert caught.value is original
        assert calls == 1
        assert evidence.execution_result is not None
        exception = evidence.execution_result["exception"]
        assert exception["class"] == "requests.exceptions.ConnectionError"
        assert _SECRET not in exception["message"]
        assert "X-Amz-Signature" not in exception["message"]
        assert exception["message"].endswith("https://example.test/x")
    finally:
        recorder.close()


def test_fail_open_collector_fault_marks_incomplete_without_changing_client_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder, task, context, trace = _make_trace(tmp_path)
    screenshot_bytes = _png_bytes((8, 9, 10))
    response = FakeResponse(b"normal-response")
    screenshot_response = FakeResponse(
        b"unused",
        json_value={"b64_png": base64.b64encode(screenshot_bytes).decode("ascii")},
    )
    calls = 0

    def fake_post(*_: Any, **__: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        return response

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(
        client_module.requests, "get", lambda *_args, **_kwargs: screenshot_response
    )
    original_snapshot = trace.serializer.snapshot

    def fail_only_request(value: Any, **kwargs: Any) -> Any:
        if isinstance(value, dict) and "device" in value and "action" in value:
            raise ValueError(f"collector failed with {_SECRET}")
        return original_snapshot(value, **kwargs)

    monkeypatch.setattr(trace.serializer, "snapshot", fail_only_request)
    client = _client()

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="gui")
            observation = client.execute_action(JSONAction(action_type="click", x=1, y=1))
            evidence = trace.finish_execution(observation=observation)

        assert calls == 1
        assert (
            observation.screenshot.tobytes() == Image.open(io.BytesIO(screenshot_bytes)).tobytes()
        )
        assert evidence.execution_result is not None
        assert evidence.execution_result["request_body_snapshot_blob"] is None
        assert (
            recorder.blob_store.read_bytes(evidence.execution_result["response_body_blob"])
            == response.content
        )
        assert task.capture_complete is False
        assert "request_body_snapshot_blob" in task.missing_artifacts
        errors = [event for event in _events(task) if event["event_type"] == "collector_error"]
        assert len(errors) == 1
        assert _SECRET not in errors[0]["payload"]["exception"]["message"]
    finally:
        recorder.close()


def test_collector_fault_still_runs_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder, _, context, trace = _make_trace(tmp_path)
    calls = 0

    def fake_post(*_: Any, **__: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(b"provider-result")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    screenshot_bytes = _png_bytes((2, 3, 4))
    monkeypatch.setattr(
        client_module.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(
            b"unused",
            json_value={"b64_png": base64.b64encode(screenshot_bytes).decode("ascii")},
        ),
    )
    monkeypatch.setattr(
        trace.serializer,
        "snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("capture failure")),
    )

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="gui")
            observation = _client().execute_action(JSONAction(action_type="click", x=2, y=3))
            evidence = trace.finish_execution(observation=observation)
        assert calls == 1
        assert (
            observation.screenshot.tobytes() == Image.open(io.BytesIO(screenshot_bytes)).tobytes()
        )
        assert evidence.execution_result is not None
        assert evidence.execution_result["request_body_snapshot_blob"] is None
    finally:
        recorder.close()


@pytest.mark.parametrize(
    "failure_point",
    ["record_gui_request", "record_gui_response", "record_screenshot_source"],
)
def test_gui_hook_fault_matches_audit_off_business_path(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    screenshot_bytes = _png_bytes((14, 15, 16))

    def run_once(*, poison_hook: str | None) -> tuple[Any, ...]:
        calls: list[Any] = []
        response = FakeResponse(b'{"result":"exact reply"}')
        screenshot_response = FakeResponse(
            b"unused",
            json_value={"b64_png": base64.b64encode(screenshot_bytes).decode("ascii")},
        )

        def fake_post(url: str, *, json: dict[str, Any]) -> FakeResponse:
            calls.append(("post", url, json))
            return response

        def fake_get(url: str, *, params: dict[str, Any]) -> FakeResponse:
            calls.append(("get", url, params))
            return screenshot_response

        monkeypatch.setattr(client_module.requests, "post", fake_post)
        monkeypatch.setattr(client_module.requests, "get", fake_get)
        for hook_name in (
            "record_gui_request",
            "record_gui_response",
            "record_screenshot_source",
        ):
            monkeypatch.setattr(client_module, hook_name, lambda *_args, **_kwargs: None)
        if poison_hook is not None:
            monkeypatch.setattr(
                client_module,
                poison_hook,
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError(f"{poison_hook} unavailable")
                ),
            )

        action = JSONAction(action_type="ask_user", text="exact question")
        action_before = action.model_dump()
        observation = _client().execute_action(action)
        return (
            observation.ask_user_response,
            observation.screenshot.tobytes(),
            calls,
            action.model_dump(),
            action_before,
        )

    assert run_once(poison_hook=None) == run_once(poison_hook=failure_point)


@pytest.mark.parametrize(
    "failure_point",
    ["record_mcp_request", "record_mcp_raw_result", "record_mcp_visible_result"],
)
def test_mcp_hook_fault_matches_audit_off_business_path(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    def run_once(*, poison_hook: str | None) -> tuple[Any, ...]:
        calls: list[Any] = []
        raw_result = {"text": "exact tool result", "nested": {"value": 1}}

        class FakeToolClient:
            def call_tool_sync(self, name: str, arguments: Any) -> Any:
                calls.append(("tool", name, arguments))
                return raw_result

        for hook_name in (
            "record_mcp_request",
            "record_mcp_raw_result",
            "record_mcp_visible_result",
        ):
            monkeypatch.setattr(client_module, hook_name, lambda *_args, **_kwargs: None)
        if poison_hook is not None:
            monkeypatch.setattr(
                client_module,
                poison_hook,
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError(f"{poison_hook} unavailable")
                ),
            )

        client = object.__new__(AndroidMCPEnvClient)
        client.tool_map = {"fixture_tool": FakeToolClient()}
        screenshot = Image.new("RGB", (2, 2), (1, 2, 3))
        client.get_screenshot = lambda **_kwargs: screenshot
        action = JSONAction(
            action_type=MCP,
            action_name="fixture_tool",
            action_json={"exact": [1, 2, 3]},
        )
        action_before = action.model_dump()
        observation = client.execute_action(action)
        return (
            observation.tool_call,
            observation.tool_call is raw_result,
            calls,
            action.model_dump(),
            action_before,
        )

    assert run_once(poison_hook=None) == run_once(poison_hook=failure_point)


def test_known_secret_response_is_not_persisted_but_live_ask_user_value_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder, task, context, trace = _make_trace(tmp_path)
    exact_reply = f"reply containing {_SECRET}"
    response = FakeResponse(
        json.dumps({"result": exact_reply}).encode(),
        headers={"Content-Type": "application/json"},
    )
    screenshot_bytes = _png_bytes((30, 40, 50))
    screenshot_response = FakeResponse(
        b"unused",
        json_value={"b64_png": base64.b64encode(screenshot_bytes).decode("ascii")},
    )
    monkeypatch.setattr(client_module.requests, "post", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(
        client_module.requests, "get", lambda *_args, **_kwargs: screenshot_response
    )

    try:
        with bind_audit_context(context):
            trace.begin_execution(execution_kind="ask_user")
            observation = _client().execute_action(
                JSONAction(action_type="ask_user", text="question")
            )
            evidence = trace.finish_execution(observation=observation)

        assert observation.ask_user_response == exact_reply
        assert evidence.execution_result is not None
        assert evidence.execution_result["response_body_blob"] is None
        assert evidence.execution_result["ask_user_response"] is None
        assert task.capture_complete is False
        for path in recorder.run_root.rglob("*"):
            if path.is_file():
                assert _SECRET.encode() not in path.read_bytes()
    finally:
        recorder.close()


def test_disabled_path_does_not_construct_or_touch_trace_and_preserves_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PoisonTrace:
        def __getattribute__(self, name: str) -> Any:
            raise AssertionError(f"disabled path touched trace.{name}")

    class PoisonSerializer:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise AssertionError("disabled path constructed a serializer")

    monkeypatch.setattr(execution_io_module, "ArtifactSerializer", PoisonSerializer)
    screenshot_bytes = _png_bytes((90, 91, 92))
    response = FakeResponse(b'{"result":"unchanged"}')
    screenshot_response = FakeResponse(
        b"unused",
        json_value={"b64_png": base64.b64encode(screenshot_bytes).decode("ascii")},
    )
    calls = {"post": 0, "get": 0}

    def fake_post(*_: Any, **__: Any) -> FakeResponse:
        calls["post"] += 1
        return response

    def fake_get(*_: Any, **__: Any) -> FakeResponse:
        calls["get"] += 1
        return screenshot_response

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.requests, "get", fake_get)
    context = AuditContext(
        run_id="disabled",
        recorder=NULL_TASK_RECORDER,
        execution_evidence_trace=PoisonTrace(),
    )
    action = JSONAction(action_type="ask_user", text="question")
    before = action.model_dump()

    with bind_audit_context(context):
        assert ExecutionEvidenceTrace.from_context() is None
        observation = _client().execute_action(action)

    assert calls == {"post": 1, "get": 1}
    assert observation.ask_user_response == "unchanged"
    assert action.model_dump() == before


def test_screenshot_source_associations_are_context_local_across_threads(
    tmp_path: Path,
) -> None:
    run_one, _, context_one, trace_one = _make_trace(tmp_path / "one", secrets=())
    run_two, _, context_two, trace_two = _make_trace(tmp_path / "two", secrets=())
    png_one = _png_bytes((1, 1, 1))
    png_two = _png_bytes((2, 2, 2))

    def decode(
        context: AuditContext,
        trace: ExecutionEvidenceTrace,
        png: bytes,
    ) -> tuple[Image.Image, bytes | None]:
        with bind_audit_context(context):
            with bind_execution_evidence_trace(trace):
                image = _client()._base64_to_pil(base64.b64encode(png).decode("ascii"))
                return image, trace.source_screenshot_bytes(image)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_one = pool.submit(decode, context_one, trace_one, png_one)
            future_two = pool.submit(decode, context_two, trace_two, png_two)
            image_one, source_one = future_one.result()
            image_two, source_two = future_two.result()

        assert source_one == png_one
        assert source_two == png_two
        assert trace_one.source_screenshot_bytes(image_two) is None
        assert trace_two.source_screenshot_bytes(image_one) is None
    finally:
        run_one.close()
        run_two.close()
