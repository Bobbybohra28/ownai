"""Agent registry: builds agent instances from specs. Add/remove agents by editing config/agents/*.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.base import BaseAgent
from app.agents.builtin.coding import CodeChangeAgent
from app.agents.builtin.critic import CriticAgent
from app.agents.builtin.debugging import DebuggingAgent
from app.agents.builtin.documentation import DocumentationAgent
from app.agents.builtin.planner import PlannerAgent
from app.agents.builtin.project_context import ProjectContextAgent
from app.agents.builtin.responder import ResponderAgent
from app.agents.builtin.security import SecurityAgent
from app.agents.builtin.sql import SQLAgent
from app.agents.runtimes.single_shot import SingleShotAgent, StreamTextAgent
from app.agents.runtimes.tool_loop import ToolLoopAgent
from app.agents.spec import AgentSpec, load_specs
from app.core.exceptions import ConfigurationError, NotFoundError
from app.tools.registry import ToolRegistry

RUNTIMES: dict[str, type[BaseAgent]] = {
    "single_shot": SingleShotAgent,
    "stream_text": StreamTextAgent,
    "tool_loop": ToolLoopAgent,
}
BUILTINS: dict[str, type[BaseAgent]] = {
    "planner": PlannerAgent,
    "project_context": ProjectContextAgent,
    "critic": CriticAgent,
    "responder": ResponderAgent,
    "code_change": CodeChangeAgent,
    "debugging": DebuggingAgent,
    "security": SecurityAgent,
    "sql": SQLAgent,
    "documentation": DocumentationAgent,
}
CORE_AGENT_IDS = {"planner", "critic", "responder"}


class AgentRegistry:
    def __init__(self, specs: list[AgentSpec], tools: ToolRegistry) -> None:
        self._specs = {s.id: s for s in specs}
        self._overrides: dict[str, bool] = {}
        for spec in specs:
            unknown = [t for t in spec.allowed_tools if not tools.has(t)]
            if unknown:
                raise ConfigurationError(f"Agent '{spec.id}' references unknown tools: {', '.join(unknown)}")
            if spec.runtime == "builtin" and spec.implementation not in BUILTINS:
                raise ConfigurationError(f"Agent '{spec.id}' has unknown implementation '{spec.implementation}'.")

    @classmethod
    def from_directory(cls, directory: Path, tools: ToolRegistry) -> AgentRegistry:
        return cls(load_specs(directory), tools)

    def apply_overrides(self, overrides: dict[str, bool]) -> None:
        self._overrides = dict(overrides)

    def spec(self, agent_id: str) -> AgentSpec:
        spec = self._specs.get(agent_id)
        if spec is None:
            raise NotFoundError(f"Unknown agent '{agent_id}'.")
        enabled = self._overrides.get(agent_id, spec.enabled)
        return spec if enabled == spec.enabled else spec.model_copy(update={"enabled": enabled})

    def has(self, agent_id: str) -> bool:
        return agent_id in self._specs and self.spec(agent_id).enabled

    def get(self, agent_id: str) -> BaseAgent:
        spec = self.spec(agent_id)
        cls = BUILTINS[spec.implementation or ""] if spec.runtime == "builtin" else RUNTIMES[spec.runtime]
        return cls(spec)

    def specs(self) -> list[AgentSpec]:
        return [self.spec(i) for i in self._specs]

    def catalog(self) -> list[dict[str, Any]]:
        """Agents the planner may choose from."""
        return [{"id": s.id, "description": s.description} for s in self.specs()
                if s.enabled and s.id not in CORE_AGENT_IDS]
