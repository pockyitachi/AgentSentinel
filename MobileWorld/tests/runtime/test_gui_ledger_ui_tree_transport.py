from __future__ import annotations

import json
import re
import subprocess
import threading
from types import SimpleNamespace

import pytest
import requests

from mobile_world.core import server
from mobile_world.runtime import client as client_module
from mobile_world.runtime import controller as controller_module
from mobile_world.runtime.client import AndroidEnvClient
from mobile_world.runtime.controller import AndroidController


def _controller() -> AndroidController:
    controller = object.__new__(AndroidController)
    controller.device = "fixture-device; not-a-shell-command"
    return controller


def _controller_result(monkeypatch, *, xml=b"<hierarchy/>", dump_code=0, read_code=0):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "uiautomator" in command:
            return SimpleNamespace(returncode=dump_code)
        if "head" in command:
            return SimpleNamespace(returncode=read_code, stdout=xml)
        assert command[-3:-1] == ["rm", "-f"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(controller_module.subprocess, "run", run)
    return _controller().get_ui_tree(), calls


def test_controller_uses_fresh_bounded_argv_and_cleans_only_owned_path(monkeypatch):
    result, calls = _controller_result(monkeypatch)
    assert result == {
        "status": "ok",
        "source": "uiautomator",
        "xml": "<hierarchy/>",
        "reason": None,
    }
    assert len(calls) == 3
    paths = [command[-1] for command, _ in calls]
    assert len(set(paths)) == 1
    assert re.fullmatch(r"/data/local/tmp/mw_ui_tree_[a-f0-9]{32}\.xml", paths[0])
    assert all(command[:3] == ["adb", "-s", _controller().device] for command, _ in calls)
    assert calls[0][0][3:-1] == ["shell", "timeout", "-s", "KILL", "5", "uiautomator", "dump"]
    assert calls[1][0][3:-1] == ["exec-out", "head", "-c", "524289"]
    assert [kwargs["timeout"] for _, kwargs in calls] == [6.0, 1.0, 1.0]
    assert all("shell" not in kwargs for _, kwargs in calls)
    _, fresh_calls = _controller_result(monkeypatch)
    assert fresh_calls[0][0][-1] != paths[0]


@pytest.mark.parametrize(
    ("arguments", "reason", "count"),
    [
        ({"dump_code": 1}, "capture_failed", 2),
        ({"read_code": 1}, "read_failed", 3),
        ({"xml": b""}, "empty_tree", 3),
        ({"xml": b"\xff"}, "invalid_encoding", 3),
        ({"xml": b"x" * (512 * 1024 + 1)}, "tree_too_large", 3),
    ],
)
def test_controller_unavailable_never_retries(monkeypatch, arguments, reason, count):
    result, calls = _controller_result(monkeypatch, **arguments)
    assert result["status"] == "unavailable"
    assert result["xml"] is None
    assert result["reason"] == reason
    assert len(calls) == count
    assert calls[-1][0][-3:-1] == ["rm", "-f"]


def test_controller_timeout_attempts_cleanup_and_does_not_leak_error(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired("SECRET command text", kwargs["timeout"])

    monkeypatch.setattr(controller_module.subprocess, "run", run)
    result = _controller().get_ui_tree()
    assert result["reason"] == "capture_timeout"
    assert len(calls) == 2
    assert "SECRET" not in repr(result)


class _Raw:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read1(self, size, *, decode_content):
        assert size == 16 * 1024
        assert decode_content is False
        return next(self.chunks, b"")


class _Response:
    def __init__(self, payload=None, *, status=200, chunks=None):
        self.status_code = status
        self.headers = {}
        self.raw = _Raw(chunks if chunks is not None else [json.dumps(payload).encode()])
        self.closed = False

    def close(self):
        self.closed = True


def _client(response):
    client = object.__new__(AndroidEnvClient)
    client.base_url = "http://fixture.invalid"
    client.device = "fixture-device"
    client._initialized = True
    client._request_deadline_monotonic_ns = None
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if isinstance(response, Exception):
            raise response
        return response

    client._session = SimpleNamespace(get=get)
    return client, calls


def _payload(**updates):
    return {
        "device": "fixture-device",
        "status": "ok",
        "source": "uiautomator",
        "xml": "<hierarchy/>",
        "reason": None,
        **updates,
    }


def test_client_success_is_one_bounded_request_no_initialization_or_retry():
    response = _Response(_payload())
    client, calls = _client(response)
    assert client.get_ui_tree() == {
        "status": "ok",
        "source": "uiautomator",
        "xml": "<hierarchy/>",
        "reason": None,
    }
    assert calls == [
        (
            "http://fixture.invalid/ui_tree",
            {
                "params": {"device": "fixture-device"},
                "timeout": 12.0,
                "stream": True,
                "allow_redirects": False,
                "headers": {"Accept-Encoding": "identity"},
            },
        )
    ]
    assert response.closed


def test_client_uninitialized_does_not_initialize_or_send_request():
    client, calls = _client(None)
    client._initialized = False
    assert client.get_ui_tree()["reason"] == "not_initialized"
    assert calls == []


def test_client_rejects_compression_before_reading_body():
    response = _Response(_payload())
    response.headers = {"Content-Encoding": "gzip"}
    response.raw = None
    client, calls = _client(response)
    assert client.get_ui_tree()["reason"] == "invalid_response"
    assert len(calls) == 1
    assert response.closed


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_Response(status=404), "unsupported_endpoint"),
        (_Response(status=503), "http_error"),
        (_Response(_payload(device="wrong")), "invalid_response"),
        (_Response(_payload(source="other")), "invalid_response"),
        (_Response(_payload(extra="unexpected")), "invalid_response"),
        (_Response(_payload(xml=None)), "invalid_response"),
        (_Response(_payload(xml="")), "invalid_response"),
        (_Response(_payload(xml="x" * (512 * 1024 + 1))), "invalid_response"),
        (_Response(_payload(status="unavailable", xml=None, reason="device_busy")), "device_busy"),
        (
            _Response(_payload(status="unavailable", xml=None, reason="SECRET error")),
            "invalid_response",
        ),
        (_Response(_payload(status="unavailable", xml=None, reason=[])), "invalid_response"),
        (_Response(chunks=[b"not JSON SECRET"]), "invalid_response"),
        (_Response(chunks=[b"x" * (6 * 512 * 1024 + 1025)]), "response_too_large"),
        (requests.Timeout("SECRET request"), "capture_timeout"),
        (requests.ConnectionError("SECRET request"), "transport_error"),
    ],
)
def test_client_failures_are_closed_and_nonfatal(response, reason):
    client, calls = _client(response)
    result = client.get_ui_tree()
    assert result == {
        "status": "unavailable",
        "source": "uiautomator",
        "xml": None,
        "reason": reason,
    }
    assert len(calls) == 1
    assert "SECRET" not in repr(result)
    if not isinstance(response, Exception):
        assert response.closed


def test_client_expired_case_deadline_sends_nothing(monkeypatch):
    client, calls = _client(None)
    client._request_deadline_monotonic_ns = 1
    monkeypatch.setattr(client_module.time, "monotonic_ns", lambda: 2)
    assert client.get_ui_tree()["reason"] == "capture_timeout"
    assert calls == []


def test_client_checks_absolute_deadline_between_reads(monkeypatch):
    response = _Response(_payload())
    client, calls = _client(response)
    ticks = iter([0.0, 13.0])
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))
    assert client.get_ui_tree()["reason"] == "capture_timeout"
    assert len(calls) == 1
    assert response.closed


def test_server_only_calls_already_initialized_controller(monkeypatch):
    monkeypatch.setattr(server, "_lifecycle_lock", threading.RLock())
    result = {"status": "ok", "source": "uiautomator", "xml": "<hierarchy/>", "reason": None}
    monkeypatch.setattr(
        server, "CONTROLLERS", {"fixture-device": SimpleNamespace(get_ui_tree=lambda: result)}
    )
    assert server.get_ui_tree("fixture-device") == {"device": "fixture-device", **result}
    assert server.get_ui_tree("other-device")["reason"] == "not_initialized"
    assert server._lifecycle_transition_snapshot() == (None, None)


def test_server_busy_lock_never_waits_or_initializes(monkeypatch):
    class _BusyLock:
        def acquire(self, *, blocking):
            assert blocking is False
            return False

        def release(self):
            pytest.fail("unowned lock was released")

    monkeypatch.setattr(server, "_lifecycle_lock", _BusyLock())
    monkeypatch.setattr(server, "CONTROLLERS", {})
    assert server.get_ui_tree("fixture-device")["reason"] == "device_busy"


def test_server_capture_exception_is_unavailable_and_releases_lock(monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(server, "_lifecycle_lock", lock)

    def fail():
        raise RuntimeError("SECRET app content")

    monkeypatch.setattr(
        server, "CONTROLLERS", {"fixture-device": SimpleNamespace(get_ui_tree=fail)}
    )
    result = server.get_ui_tree("fixture-device")
    assert result["reason"] == "capture_failed"
    assert "SECRET" not in repr(result)
    assert not lock.locked()


def test_xml_size_bound_agrees_between_client_and_controller():
    assert client_module._UI_TREE_MAX_XML_BYTES == controller_module.UI_TREE_MAX_XML_BYTES
