"""AgentSpec: declarative agent definitions loaded from config/agents/*.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.core.exceptions import ConfigurationError
from app.models.providers.base import ModelRole

Runtime = Literal["single_shot", "stream_text", "tool_loop", "builtin"]


class VerificationRules(BaseModel):
    require_citations: bool = False   # answers must cite retrieved sources
    run_tests: bool = False           # run the test suite against produced changes
    run_lint: bool = False            # lint produced changes
    syntax_check: bool = False        # parse changed files
    critic: bool = True               # independent critic review in modes that enable it


class AgentSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,59}$")
    name: str
    description: str
    category: Literal["core", "development", "data", "devops", "security", "knowledge"]
    capabilities: list[str] = Field(default_factory=list)
    runtime: Runtime
    implementation: str | None = None  # builtin class key (runtime == builtin)
    input_schema: str = "AgentTask"
    output_schema: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    model_role: ModelRole = ModelRole.CODING
    complex_model_role: ModelRole | None = None  # role for complex tasks (e.g. reasoning)
    system_prompt: str = ""
    max_iterations: int = Field(default=6, ge=1, le=30)
    max_output_tokens: int = Field(default=2048, ge=64, le=32_000)
    temperature: float = Field(default=0.2, ge=0, le=2)
    timeout_s: int = Field(default=600, ge=5, le=3600)
    context_budget_tokens: int = Field(default=3000, ge=0)
    verification: VerificationRules = Field(default_factory=VerificationRules)
    requires_project: bool = False
    min_plan: str = "free"
    enabled: bool = True

    def role_for(self, complexity: str) -> ModelRole:
        if complexity == "complex" and self.complex_model_role:
            return self.complex_model_role
        return self.model_role


def load_specs(directory: Path) -> list[AgentSpec]:
    if not directory.is_dir():
        raise ConfigurationError(f"Agent configuration directory not found: {directory}")
    specs: list[AgentSpec] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            specs.append(AgentSpec.model_validate(data))
        except (yaml.YAMLError, ValidationError) as exc:
            raise ConfigurationError(f"Invalid agent spec {path.name}.", detail=str(exc)) from exc
    ids = [s.id for s in specs]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ConfigurationError(f"Duplicate agent ids: {', '.join(sorted(duplicates))}")
    return specs
