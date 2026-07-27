"""Standalone reliability-oriented agent for multi-application SaaS tasks."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from saas_agent.models import AgentConfig, AgentTask

__all__ = ["AgentConfig", "AgentTask", "run_agent"]
__version__ = "0.1.0b0"


async def run_agent(
    task: AgentTask | Mapping[str, Any],
    config: AgentConfig | None = None,
    *,
    system_rules: str | None = None,
    tool_context: Mapping[str, Any] | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Import the browser runtime lazily and execute one task."""

    from saas_agent.agent import run_agent as _run_agent

    return await _run_agent(
        task,
        config,
        system_rules=system_rules,
        tool_context=tool_context,
        output_path=output_path,
    )
