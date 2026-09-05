from __future__ import annotations

import logging
from collections.abc import Callable
from io import StringIO
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from loguru import logger
from openai import OpenAI

from mobile_world.agents.base import BaseAgent, MCPAgent
from mobile_world.agents.implementations.mai_ui_agent import MAIUINaivigationAgent
from mobile_world.runtime.utils.models import JSONAction

TASK_INSTRUCTION_TOKEN = "SYNTHETIC_TASK_INSTRUCTION_TOKEN_R24_P1"
EVIDENCE_TOKEN = "SYNTHETIC_EVIDENCE_TOKEN_R24_SDK_LOG"
IMAGE_TOKEN = "SYNTHETIC_IMAGE_TOKEN_R24_SDK_LOG"
API_KEY_TOKEN = "SYNTHETIC_API_KEY_TOKEN_R24_SDK_LOG"


class _MCPAgent(MCPAgent):
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        del observation
        raise NotImplementedError


class _ActorLoggingAgent(_MCPAgent):
    def __init__(self, *, strict_provider_audit: bool) -> None:
        super().__init__(tools=[])
        self._strict_provider_audit = strict_provider_audit

    def _production_safe_logging_active(self) -> bool:
        return self._strict_provider_audit


def _mcp_agent(*, strict_provider_audit: bool) -> MCPAgent:
    return _MCPAgent(
        tools=[],
        prompt_sentinel=SimpleNamespace(strict_provider_audit=strict_provider_audit),
    )


def _mai_agent(*, strict_provider_audit: bool) -> MCPAgent:
    agent = MAIUINaivigationAgent.__new__(MAIUINaivigationAgent)
    BaseAgent.__init__(
        agent,
        prompt_sentinel=SimpleNamespace(strict_provider_audit=strict_provider_audit),
    )
    agent.tools = []
    return agent


def _initialize_log(agent: MCPAgent) -> str:
    output = StringIO()
    sink_id = logger.add(output, format="{message}")
    try:
        assert agent.initialize(TASK_INSTRUCTION_TOKEN)
    finally:
        logger.remove(sink_id)
    return output.getvalue()


def _actor_logging_client() -> OpenAI:
    def respond(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.connection").debug("request headers: %s", dict(request.headers))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-r24-sdk-log",
                "object": "chat.completion",
                "created": 0,
                "model": "cpu-fake-actor",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    return OpenAI(
        api_key=API_KEY_TOKEN,
        base_url="http://127.0.0.1:1/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )


def _call_actor_with_request_bearing_sdk_logs(*, strict_provider_audit: bool) -> str:
    agent = _ActorLoggingAgent(strict_provider_audit=strict_provider_audit)
    client = _actor_logging_client()
    agent.openai_client = client
    try:
        result = agent.openai_chat_completions_create(
            model="cpu-fake-actor",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{TASK_INSTRUCTION_TOKEN} {EVIDENCE_TOKEN}",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{IMAGE_TOKEN}",
                            },
                        },
                    ],
                }
            ],
            retry_times=1,
        )
    finally:
        client.close()
    assert type(result) is str
    return result


@pytest.mark.parametrize(
    ("agent_factory", "safe_message"),
    (
        (_mcp_agent, "Initialized production MCP actor instruction"),
        (_mai_agent, "Initializing production MAI UI agent"),
    ),
)
def test_strict_provider_audit_suppresses_task_instruction_in_ordinary_logs(
    agent_factory: Callable[..., MCPAgent],
    safe_message: str,
) -> None:
    output = _initialize_log(agent_factory(strict_provider_audit=True))

    assert TASK_INSTRUCTION_TOKEN not in output
    assert safe_message in output


@pytest.mark.parametrize(
    ("agent_factory", "ordinary_message"),
    (
        (_mcp_agent, "initialized the agent with the given instruction"),
        (_mai_agent, "Initializing MAI UI agent with instruction"),
    ),
)
def test_nonproduction_initialize_logging_remains_detailed(
    agent_factory: Callable[..., MCPAgent],
    ordinary_message: str,
) -> None:
    output = _initialize_log(agent_factory(strict_provider_audit=False))

    assert TASK_INSTRUCTION_TOKEN in output
    assert ordinary_message in output


def test_strict_actor_call_suppresses_openai_http_debug_request_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OPENAI_LOG", "debug")
    caplog.clear()
    with (
        caplog.at_level(logging.DEBUG, logger="openai._base_client"),
        caplog.at_level(logging.DEBUG, logger="httpcore.connection"),
    ):
        assert _call_actor_with_request_bearing_sdk_logs(strict_provider_audit=True) == "ok"
        logging.getLogger("openai._base_client").debug("NONPRODUCTION_SDK_LOG_CANARY")

    emitted = caplog.text
    assert "NONPRODUCTION_SDK_LOG_CANARY" in emitted
    assert TASK_INSTRUCTION_TOKEN not in emitted
    assert EVIDENCE_TOKEN not in emitted
    assert IMAGE_TOKEN not in emitted
    assert API_KEY_TOKEN not in emitted


def test_strict_actor_client_construction_suppresses_sdk_debug_options(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from mobile_world.agents import base as base_module

    endpoint_token = "http://127.0.0.1:1/SYNTHETIC_ENDPOINT_TOKEN_R24"

    def construct(**kwargs: object) -> object:
        logging.getLogger("openai._base_client").debug("Client options: %s", kwargs)
        return object()

    monkeypatch.setattr(base_module, "OpenAI", construct)
    monkeypatch.setenv("OPENAI_LOG", "debug")
    agent = _ActorLoggingAgent(strict_provider_audit=True)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="openai._base_client"):
        agent.build_openai_client(endpoint_token, API_KEY_TOKEN)
        logging.getLogger("openai._base_client").debug("POST_CONSTRUCTION_LOG_CANARY")

    assert "POST_CONSTRUCTION_LOG_CANARY" in caplog.text
    assert endpoint_token not in caplog.text
    assert API_KEY_TOKEN not in caplog.text


def test_nonproduction_actor_call_retains_openai_debug_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OPENAI_LOG", "debug")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="openai._base_client"):
        assert _call_actor_with_request_bearing_sdk_logs(strict_provider_audit=False) == "ok"

    assert TASK_INSTRUCTION_TOKEN in caplog.text
    assert EVIDENCE_TOKEN in caplog.text
    assert IMAGE_TOKEN in caplog.text
