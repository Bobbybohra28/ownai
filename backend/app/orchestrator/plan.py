"""Execution plans: deterministic workflow templates + validation of model-produced plans."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.registry import AgentRegistry
from app.agents.schemas import PlanOutput
from app.core.exceptions import ErrorCode, OrchestratorError


class PlanStep(BaseModel):
    id: str
    agent: str
    goal: str
    depends_on: list[str] = Field(default_factory=list)
    produces_changes: bool = False


class Plan(BaseModel):
    intent: str
    source: str  # template | planner | planner_fallback
    steps: list[PlanStep]
    success_criteria: list[str] = Field(default_factory=list)
    critic: bool = True

    def order(self) -> list[list[PlanStep]]:
        """Topological layers (steps in a layer can run concurrently)."""
        remaining = {s.id: s for s in self.steps}
        done: set[str] = set()
        layers: list[list[PlanStep]] = []
        while remaining:
            layer = [s for s in remaining.values() if set(s.depends_on) <= done]
            if not layer:
                raise OrchestratorError("The plan has circular dependencies.", code=ErrorCode.PLAN_INVALID)
            layers.append(layer)
            for s in layer:
                done.add(s.id)
                remaining.pop(s.id)
        return layers


CHANGE_AGENTS = {"coding", "refactoring", "testing"}

_TEMPLATES: dict[str, list[tuple[str, str, list[str]]]] = {
    # intent: [(step_id, agent, depends_on)]
    "explain": [("context", "project_context", []), ("answer", "rag", ["context"])],
    "search_code": [("context", "project_context", []), ("answer", "rag", ["context"])],
    "fix_bug": [("context", "project_context", []), ("diagnose", "debugging", ["context"]),
                ("fix", "coding", ["diagnose"])],
    "debug": [("context", "project_context", []), ("diagnose", "debugging", ["context"])],
    "analyze_logs": [("context", "project_context", []), ("diagnose", "debugging", ["context"])],
    "generate_code": [("context", "project_context", []), ("implement", "coding", ["context"])],
    "modify_code": [("context", "project_context", []), ("implement", "coding", ["context"])],
    "refactor": [("context", "project_context", []), ("refactor", "refactoring", ["context"])],
    "write_tests": [("context", "project_context", []), ("tests", "testing", ["context"])],
    "run_tests": [("context", "project_context", []), ("diagnose", "debugging", ["context"])],
    "review": [("context", "project_context", []), ("review", "code_review", ["context"])],
    "architecture": [("context", "project_context", []), ("architecture", "architecture", ["context"])],
    "documentation": [("context", "project_context", []), ("docs", "documentation", ["context"])],
    "sql": [("context", "project_context", []), ("sql", "sql", ["context"])],
    "git": [("git", "git", [])],
    "docker": [("context", "project_context", []), ("docker", "docker", ["context"])],
    "devops": [("context", "project_context", []), ("devops", "devops", ["context"])],
    "security": [("context", "project_context", []), ("security", "security", ["context"])],
    "data_analysis": [("context", "project_context", []), ("analysis", "data_analysis", ["context"])],
    "general": [("answer", "assistant", [])],
}
_NO_PROJECT = {"generate_code": "assistant", "modify_code": "assistant", "explain": "assistant",
               "search_code": "assistant", "architecture": "architecture", "documentation": "assistant",
               "sql": "sql", "devops": "devops", "data_analysis": "data_analysis", "review": "assistant",
               "refactor": "assistant", "write_tests": "assistant", "fix_bug": "assistant", "debug": "assistant"}

_GOALS = {
    "project_context": "Retrieve the project files and documentation relevant to the request.",
    "rag": "Answer the question from the retrieved project context, citing sources.",
    "debugging": "Reproduce the problem and identify its root cause with evidence.",
    "coding": "Implement the change / fix with minimal, correct edits.",
    "refactoring": "Refactor the code without changing behaviour.",
    "testing": "Write tests for the relevant code and make sure they run.",
    "code_review": "Review the relevant code and report concrete issues.",
    "architecture": "Explain and assess the architecture and give recommendations.",
    "documentation": "Write the requested documentation.",
    "sql": "Write, validate and (if possible) run the SQL.",
    "git": "Inspect the repository and answer the Git request.",
    "docker": "Analyse the Docker setup.",
    "devops": "Analyse CI/CD, deployment and configuration.",
    "security": "Scan the project for security issues and report them with evidence.",
    "data_analysis": "Analyse the data with sandboxed Python.",
    "assistant": "Answer the request directly.",
}

_CRITERIA = {
    "explain": ["Answer is grounded in retrieved project files", "Key files/functions are cited"],
    "fix_bug": ["Root cause is identified with evidence", "Fix addresses the root cause",
                "Existing tests pass with the change"],
    "generate_code": ["Requested functionality is implemented", "Code is consistent with the project"],
}


def template_plan(intent: str, *, has_project: bool, registry: AgentRegistry, critic: bool) -> Plan:
    steps_def = _TEMPLATES.get(intent) or _TEMPLATES["general"]
    if not has_project:
        agent = _NO_PROJECT.get(intent, "assistant")
        steps_def = [("answer", agent, [])]
    steps: list[PlanStep] = []
    for step_id, agent_id, deps in steps_def:
        if not registry.has(agent_id):
            continue  # disabled agents are skipped; dependencies are pruned below
        steps.append(PlanStep(id=step_id, agent=agent_id, goal=_GOALS.get(agent_id, "Complete the task."),
                              depends_on=deps, produces_changes=agent_id in CHANGE_AGENTS))
    ids = {s.id for s in steps}
    for s in steps:
        s.depends_on = [d for d in s.depends_on if d in ids]
    if not steps:
        raise OrchestratorError("No enabled agent can handle this request.", code=ErrorCode.PLAN_INVALID)
    return Plan(intent=intent, source="template", steps=steps, success_criteria=_CRITERIA.get(intent, []),
                critic=critic and registry.has("critic"))


def validate_planner_output(output: PlanOutput, *, intent: str, registry: AgentRegistry, has_project: bool,
                            max_steps: int, critic: bool) -> Plan:
    if not output.steps:
        raise OrchestratorError("The planner returned no steps.", code=ErrorCode.PLAN_INVALID)
    if len(output.steps) > max_steps:
        raise OrchestratorError(f"The planner returned {len(output.steps)} steps (max {max_steps}).",
                                code=ErrorCode.PLAN_INVALID)
    seen: set[str] = set()
    steps: list[PlanStep] = []
    catalog = {a["id"] for a in registry.catalog()}
    for raw in output.steps:
        step_id = raw.id.strip() or f"s{len(steps) + 1}"
        if step_id in seen:
            raise OrchestratorError(f"Duplicate step id '{step_id}'.", code=ErrorCode.PLAN_INVALID)
        if raw.agent not in catalog:
            raise OrchestratorError(f"The planner chose an unknown or disabled agent '{raw.agent}'.",
                                    code=ErrorCode.PLAN_INVALID)
        spec = registry.spec(raw.agent)
        if spec.requires_project and not has_project:
            raise OrchestratorError(f"Agent '{raw.agent}' needs a project.", code=ErrorCode.PLAN_INVALID)
        unknown_deps = [d for d in raw.depends_on if d not in seen]
        if unknown_deps:
            raise OrchestratorError(f"Step '{step_id}' depends on unknown/later steps {unknown_deps}.",
                                    code=ErrorCode.PLAN_INVALID)
        seen.add(step_id)
        steps.append(PlanStep(id=step_id, agent=raw.agent, goal=raw.goal[:1000], depends_on=raw.depends_on,
                              produces_changes=raw.agent in CHANGE_AGENTS))
    if sum(1 for s in steps if s.produces_changes) > 2:
        raise OrchestratorError("The plan modifies code in too many independent steps.", code=ErrorCode.PLAN_INVALID)
    plan = Plan(intent=intent, source="planner", steps=steps, success_criteria=output.success_criteria[:10],
                critic=critic and registry.has("critic"))
    plan.order()  # raises on cycles
    return plan
