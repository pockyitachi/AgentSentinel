from __future__ import annotations

from collections.abc import Callable
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from mobile_world.agents.base import BaseAgent, MCPAgent
from mobile_world.agents.implementations.mai_ui_agent import MAIUINaivigationAgent
from mobile_world.runtime.utils.models import JSONAction

TASK_INSTRUCTION_TOKEN = "SYNTHETIC_TASK_INSTRUCTION_TOKEN_R24_P1"


class _MCPAgent(MCPAgent):
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        del observation
        raise NotImplementedError


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
