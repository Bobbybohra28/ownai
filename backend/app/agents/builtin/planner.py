"""Planner agent: decomposes complex requests into a validated plan of agent steps.

Simple, well-understood intents use deterministic workflow templates (fast path, no
model call). The model-based planner is used for Deep Analysis and unrecognised or
multi-part requests; its plan is validated before execution (known, enabled agents;
acyclic dependencies; step limit) and falls back to the template if invalid.
"""

from __future__ import annotations

import json

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent
from app.agents.runtimes.single_shot import output_instructions, structured_call
from app.agents.schemas import PlanOutput
from app.models.providers.base import ChatMessage


class PlannerAgent(BaseAgent):
    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        await ctx.status("Planning…")
        catalog = task.inputs.get("agents") or []
        max_steps = int(task.inputs.get("max_steps") or 6)
        guidance = (
            "Available agents (id: description):\n" + "\n".join(f"- {a['id']}: {a['description']}" for a in catalog)
            + f"\n\nCreate a plan of at most {max_steps} steps using ONLY these agent ids. "
            "Use the fewest agents that fully solve the request. Put 'project_context' first when project knowledge "
            "is needed. Do not add critic or responder steps (the platform adds verification automatically). "
            "Each step's goal must be specific. List measurable success criteria.\n"
            + output_instructions(PlanOutput)
        )
        messages = [
            ChatMessage(role="system", content=self.system_prompt(guidance)),
            ChatMessage(role="user", content=f"Request: {task.request}\nDetected intent: {task.intent}\n"
                        f"Project: {'yes — ' + ctx.project.name if ctx.project else 'none'}\n"
                        f"Template plan for reference: {json.dumps(task.inputs.get('template') or [])}"),
        ]
        plan = await structured_call(self, ctx, task, messages, PlanOutput, result)
        result.output = plan.model_dump()
        result.summary = f"{len(plan.steps)} step plan: " + " → ".join(s.agent for s in plan.steps)
