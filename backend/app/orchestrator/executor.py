"""Orchestrator: runs a task end-to-end.

User request → intent → mode → plan → agents (+tools) → verification (syntax/tests/lint,
fix loop) → critic → approval checkpoint → (resume) apply → post-apply tests →
final report built from the evidence ledger.

State is checkpointed in ``task_runs.state`` so a run can pause for human approval
and resume in any worker process.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.base import AgentContext, AgentResult, AgentTask
from app.agents.builtin.critic import critic_inputs
from app.agents.builtin.responder import compose_report
from app.agents.registry import AgentRegistry
from app.agents.schemas import PlanOutput
from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode, OrchestratorError
from app.core.logging import bind_contextvars, get_logger, unbind_contextvars
from app.database.base import utcnow
from app.database.models import (
    Approval,
    ApprovalStatus,
    ChangeSet,
    Message,
    OrgRole,
    Project,
    RunEvent,
    RunStep,
    Task,
    TaskRun,
    TaskStatus,
)
from app.database.repositories.identity import OrganizationRepository
from app.memory.manager import MemoryManager
from app.models.router import ModelRouter
from app.orchestrator import verification
from app.orchestrator.events import EventBus, RunEventEmitter
from app.orchestrator.evidence import EvidenceItem, EvidenceLedger
from app.orchestrator.intent import IntentResult, detect_intent
from app.orchestrator.modes import ModePolicy, resolve_mode
from app.orchestrator.plan import Plan, PlanStep, template_plan, validate_planner_output
from app.rag.retrieval import HybridRetriever
from app.tools.base import ProjectRef, ToolServices
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

log = get_logger(__name__)

STATUS_BY_AGENT = {
    "project_context": "Searching project…", "rag": "Writing the answer…", "debugging": "Investigating the problem…",
    "coding": "Preparing code changes…", "refactoring": "Refactoring…", "testing": "Writing tests…",
    "code_review": "Reviewing code…", "architecture": "Analysing architecture…", "documentation": "Writing documentation…",
    "sql": "Working on SQL…", "git": "Inspecting Git…", "docker": "Analysing Docker setup…",
    "devops": "Analysing DevOps setup…", "security": "Reviewing security…", "research": "Researching…",
    "data_analysis": "Analysing data…", "assistant": "Thinking…",
}


@dataclass
class OrchestratorDeps:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    router: ModelRouter
    agents: AgentRegistry
    tools: ToolExecutor
    tool_registry: ToolRegistry
    tool_services: ToolServices
    retriever: HybridRetriever | None
    events: EventBus
    memory: MemoryManager


class RunState:
    """Serializable run checkpoint."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.intent: dict[str, Any] = data.get("intent", {})
        self.mode: str = data.get("mode", "developer")
        self.plan: dict[str, Any] = data.get("plan", {})
        self.steps: dict[str, dict[str, Any]] = data.get("steps", {})
        self.context: str = data.get("context", "")
        self.citations: list[dict[str, Any]] = data.get("citations", [])
        self.changeset_id: str | None = data.get("changeset_id")
        self.critic: dict[str, Any] | None = data.get("critic")
        self.notices: list[str] = data.get("notices", [])
        self.pending: list[str] = data.get("pending", [])
        self.primary_step: str | None = data.get("primary_step")
        self.producer_models: list[str] = data.get("producer_models", [])
        self.evidence = EvidenceLedger.model_validate(data.get("evidence") or {"items": []})
        self.models: list[str] = data.get("models", [])
        self.agents: list[str] = data.get("agents", [])

    def dump(self) -> dict[str, Any]:
        return {
            "intent": self.intent, "mode": self.mode, "plan": self.plan, "steps": self.steps, "context": self.context,
            "citations": self.citations, "changeset_id": self.changeset_id, "critic": self.critic,
            "notices": self.notices, "pending": self.pending, "primary_step": self.primary_step,
            "producer_models": self.producer_models, "evidence": self.evidence.model_dump(), "models": self.models,
            "agents": self.agents,
        }

    def note(self, message: str) -> None:
        if message and message not in self.notices:
            self.notices.append(message)


class Orchestrator:
    def __init__(self, deps: OrchestratorDeps) -> None:
        self.d = deps

    # ------------------------------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------------------------------
    def _emitter(self, run_id: uuid.UUID) -> RunEventEmitter:
        async def persist(rid: uuid.UUID, seq: int, event_type: str, data: dict[str, Any]) -> None:
            async with self.d.sessions() as session:
                session.add(RunEvent(run_id=rid, seq=seq, type=event_type, payload=data, created_at=utcnow()))
                await session.commit()

        return RunEventEmitter(self.d.events, run_id, persist)

    async def _load(self, run_id: uuid.UUID) -> tuple[TaskRun, Task, Project | None, OrgRole]:
        async with self.d.sessions() as session:
            run = await session.get(TaskRun, run_id)
            if run is None:
                raise OrchestratorError("Run not found.", code=ErrorCode.NOT_FOUND)
            task = await session.get(Task, run.task_id)
            assert task is not None
            project = await session.get(Project, task.project_id) if task.project_id else None
            membership = await OrganizationRepository(session).membership(task.organization_id, task.user_id)
            role = membership.role if membership else OrgRole.VIEWER
            return run, task, project, role

    def _context(self, run: TaskRun, task: Task, project: Project | None, role: OrgRole, events: RunEventEmitter,
                 state: RunState, mode: str) -> AgentContext:
        project_ref = None
        if project is not None:
            project_ref = ProjectRef(id=project.id, organization_id=project.organization_id, name=project.name,
                                     root=Path(project.storage_path), overview=project.overview or {})

        async def cancelled() -> bool:
            async with self.d.sessions() as session:
                value = (await session.execute(select(TaskRun.cancel_requested).where(TaskRun.id == run.id))).scalar()
                return bool(value)

        return AgentContext(
            run_id=run.id, org_id=task.organization_id, user_id=task.user_id, role=role, settings=self.d.settings,
            router=self.d.router, tools=self.d.tools, tool_registry=self.d.tool_registry,
            tool_services=self.d.tool_services, events=events, evidence=state.evidence, project=project_ref,
            retriever=self.d.retriever, mode=mode, is_cancelled=cancelled,
        )

    async def _save(self, run_id: uuid.UUID, state: RunState, **fields: Any) -> None:
        async with self.d.sessions() as session:
            await session.execute(update(TaskRun).where(TaskRun.id == run_id).values(state=state.dump(), **fields))
            await session.commit()

    async def _set_task(self, task_id: uuid.UUID, **fields: Any) -> None:
        async with self.d.sessions() as session:
            await session.execute(update(Task).where(Task.id == task_id).values(**fields))
            await session.commit()

    async def _record_step(self, run_id: uuid.UUID, step: PlanStep, result: AgentResult | None, *, status: str) -> None:
        async with self.d.sessions() as session:
            row = (await session.execute(select(RunStep).where(RunStep.run_id == run_id,
                                                               RunStep.step_key == step.id))).scalar_one_or_none()
            if row is None:
                row = RunStep(run_id=run_id, step_key=step.id, agent_id=step.agent, goal=step.goal, started_at=utcnow())
                session.add(row)
            row.status = status
            if result is not None:
                row.model_id = result.model_ids[-1] if result.model_ids else None
                row.summary = result.summary[:4000]
                row.output = _jsonable(result.output)
                row.error_code = result.error_code
                row.error_message = result.error_message
                row.tools_used = [c.model_dump() for c in result.tool_calls]
                row.finished_at = utcnow()
            await session.commit()

    # ------------------------------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------------------------------
    async def start(self, run_id: uuid.UUID) -> None:
        bind_contextvars(run_id=str(run_id))
        try:
            await asyncio.wait_for(self._start(run_id), timeout=self.d.settings.max_run_seconds)
        except TimeoutError:
            await self._fail(run_id, ErrorCode.RUN_BUDGET_EXCEEDED,
                             f"The task exceeded the maximum run time ({self.d.settings.max_run_seconds}s) and was stopped.")
        finally:
            unbind_contextvars("run_id")

    async def resume(self, run_id: uuid.UUID, approval_id: uuid.UUID) -> None:
        bind_contextvars(run_id=str(run_id))
        try:
            await asyncio.wait_for(self._resume(run_id, approval_id), timeout=self.d.settings.max_run_seconds)
        except TimeoutError:
            await self._fail(run_id, ErrorCode.RUN_BUDGET_EXCEEDED, "Resuming the task took too long and was stopped.")
        finally:
            unbind_contextvars("run_id")

    async def _fail(self, run_id: uuid.UUID, code: ErrorCode | str, message: str, *, hint: str | None = None) -> None:
        events = self._emitter(run_id)
        run, task, _, _ = await self._load(run_id)
        text = f"**The task could not be completed.** {message}" + (f"\n\n_Hint: {hint}_" if hint else "")
        async with self.d.sessions() as session:
            await session.execute(update(TaskRun).where(TaskRun.id == run_id).values(
                status=TaskStatus.FAILED.value, error_code=str(code), error_message=message, finished_at=utcnow(),
                result={"error": {"code": str(code), "message": message, "hint": hint}}))
            await session.execute(update(Task).where(Task.id == task.id).values(
                status=TaskStatus.FAILED.value, finished_at=utcnow()))
            if task.conversation_id:
                session.add(Message(organization_id=task.organization_id, conversation_id=task.conversation_id,
                                    role="assistant", content=text, run_id=run_id, created_at=utcnow(),
                                    meta={"error": {"code": str(code), "message": message}}))
            await session.commit()
        await events.emit("error", code=str(code), message=message, hint=hint)
        await events.emit("run_finished", status=TaskStatus.FAILED.value, error_code=str(code))

    async def _start(self, run_id: uuid.UUID) -> None:
        events = self._emitter(run_id)
        run, task, project, role = await self._load(run_id)
        if run.status not in (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value):
            log.info("orchestrator.skip_start", status=run.status)
            return
        bind_contextvars(task_id=str(task.id), user_id=str(task.user_id), org_id=str(task.organization_id),
                         project_id=str(task.project_id) if task.project_id else None,
                         conversation_id=str(task.conversation_id) if task.conversation_id else None)
        state = RunState()
        await self._save(run_id, state, status=TaskStatus.RUNNING.value, started_at=utcnow())
        await self._set_task(task.id, status=TaskStatus.RUNNING.value)
        await events.emit("run_started", task_id=str(task.id), mode=task.mode)
        try:
            await self._execute(run, task, project, role, events, state)
        except AppError as exc:
            log.warning("orchestrator.failed", code=exc.code, error=exc.message, detail=exc.detail)
            await self._save(run_id, state)
            await self._fail(run_id, exc.code, exc.message, hint=exc.hint)
        except Exception:
            log.exception("orchestrator.crashed")
            await self._save(run_id, state)
            await self._fail(run_id, ErrorCode.ORCHESTRATOR_ERROR,
                             "An internal error interrupted the task. The technical details were logged.")

    # ------------------------------------------------------------------------------------------
    async def _execute(self, run: TaskRun, task: Task, project: Project | None, role: OrgRole,
                       events: RunEventEmitter, state: RunState) -> None:
        has_project = project is not None and project.status == "ready"
        if project is not None and not has_project:
            state.note(f"Project '{project.name}' is not indexed yet ({project.status}); project context is unavailable.")
            project = None
        await events.status("Understanding the request…")
        forced = resolve_mode(task.mode, intent="general", complexity="simple", has_project=has_project).forced_intent
        intent = IntentResult(forced, "moderate", 1.0, "mode") if forced else await detect_intent(
            task.request, has_project=has_project, router=self.d.router)
        policy = resolve_mode(task.mode, intent=intent.intent, complexity=intent.complexity, has_project=has_project)
        state.intent, state.mode = intent.to_dict(), policy.mode.value
        await events.emit("intent", **intent.to_dict(), mode=policy.mode.value)
        await self._set_task(task.id, intent=intent.intent)
        ctx = self._context(run, task, project, role, events, state, policy.mode.value)
        memory = await self.d.memory.for_task(org_id=task.organization_id, user_id=task.user_id,
                                              project_id=project.id if project else None,
                                              conversation_id=task.conversation_id, message_id=task.message_id,
                                              query=task.request)
        plan = await self._plan(task, intent, policy, has_project, ctx, state, memory.text)
        state.plan = plan.model_dump()
        await self._set_task(task.id, plan=state.plan)
        await events.emit("plan", source=plan.source, steps=[s.model_dump() for s in plan.steps],
                          success_criteria=plan.success_criteria)
        await self._run_steps(plan, task, intent, policy, ctx, state, memory.history, memory.text)
        await self._save(run.id, state)
        if plan.critic and policy.critic:
            await self._critic(task, intent, ctx, state, plan)
        # approval checkpoint for staged changes
        if state.changeset_id and await self._changeset_has_files(state.changeset_id):
            outcome = await ctx.tools.invoke("apply_changeset", {"changeset_id": state.changeset_id},
                                             ctx.tool_context(agent_id="orchestrator", step_key="apply"))
            if outcome.approval_id:
                state.pending.append(outcome.approval_id)
        if state.pending:
            await self._pause_for_approval(run, task, ctx, state)
            return
        await self._finish(run, task, ctx, state)

    async def _plan(self, task: Task, intent: IntentResult, policy: ModePolicy, has_project: bool, ctx: AgentContext,
                    state: RunState, memory_text: str) -> Plan:
        template = template_plan(intent.intent, has_project=has_project, registry=self.d.agents, critic=policy.critic)
        wants_planner = policy.use_llm_planner and has_project and self.d.agents.has("planner")
        if not wants_planner:
            return template
        planner = self.d.agents.get("planner")
        result = await planner.run(AgentTask(
            step_id="plan", goal="Plan the work", request=task.request, intent=intent.intent,
            complexity=intent.complexity, memory=memory_text,
            inputs={"agents": self.d.agents.catalog(), "max_steps": policy.max_steps,
                    "template": [s.model_dump(include={"id", "agent", "depends_on"}) for s in template.steps]},
        ), ctx)
        if result.status != "succeeded":
            state.note(f"The planner was unavailable ({result.error_message}); a standard workflow was used.")
            return template.model_copy(update={"source": "planner_fallback"})
        try:
            return validate_planner_output(PlanOutput.model_validate(result.output), intent=intent.intent,
                                           registry=self.d.agents, has_project=has_project,
                                           max_steps=policy.max_steps, critic=policy.critic)
        except AppError as exc:
            state.note(f"The generated plan was rejected ({exc.message}); a standard workflow was used.")
            return template.model_copy(update={"source": "planner_fallback"})

    async def _run_steps(self, plan: Plan, task: Task, intent: IntentResult, policy: ModePolicy, ctx: AgentContext,
                         state: RunState, history: list[dict[str, str]], memory_text: str) -> None:
        for layer in plan.order():
            for step in layer:  # sequential: steps share evidence/context and small local GPUs
                if ctx.is_cancelled and await ctx.is_cancelled():
                    raise OrchestratorError("The task was cancelled.", code=ErrorCode.RUN_CANCELLED)
                if step.id in state.steps and state.steps[step.id].get("status") == "succeeded":
                    continue
                await self._run_step(step, plan, task, intent, policy, ctx, state, history, memory_text)

    async def _run_step(self, step: PlanStep, plan: Plan, task: Task, intent: IntentResult, policy: ModePolicy,
                        ctx: AgentContext, state: RunState, history: list[dict[str, str]], memory_text: str) -> None:
        agent = self.d.agents.get(step.agent)
        inputs = {dep: state.steps.get(dep, {}).get("output") for dep in step.depends_on}
        agent_task = AgentTask(step_id=step.id, goal=step.goal, request=task.request, intent=intent.intent,
                               complexity=intent.complexity, inputs=inputs, context=state.context,
                               citations=state.citations, history=history, memory=memory_text)
        await ctx.status(STATUS_BY_AGENT.get(step.agent, f"Running {agent.spec.name}…"))
        await ctx.emit("step_started", step=step.id, agent=step.agent, agent_name=agent.spec.name, goal=step.goal)
        await self._record_step(ctx.run_id, step, None, status="running")  # type: ignore[arg-type]
        try:
            result = await asyncio.wait_for(agent.run(agent_task, ctx), timeout=agent.spec.timeout_s)
        except TimeoutError:
            result = AgentResult(agent_id=step.agent, status="failed", error_code=str(ErrorCode.AGENT_FAILED),
                                 error_message=f"{agent.spec.name} did not finish within {agent.spec.timeout_s}s.")
        # generate → test → fix loop for code changes
        if result.status == "succeeded" and step.produces_changes and result.changeset_id:
            result = await self._verify_changes(step, agent_task, result, policy, ctx, state)
        await self._absorb(step, result, state, ctx)
        await self._record_step(ctx.run_id, step, result, status=result.status)  # type: ignore[arg-type]
        await ctx.emit("step_finished", step=step.id, agent=step.agent, status=result.status,
                       summary=result.summary[:500], models=result.model_ids[-2:], duration_ms=result.duration_ms,
                       tools=[c.tool for c in result.tool_calls], error=result.error_message)
        if result.status == "failed":
            critical = step.agent not in ("project_context",)
            if critical:
                raise OrchestratorError(f"{agent.spec.name} failed: {result.error_message}",
                                        code=result.error_code or ErrorCode.AGENT_FAILED)
            state.note(f"{agent.spec.name} failed ({result.error_message}); continuing without it.")

    async def _verify_changes(self, step: PlanStep, agent_task: AgentTask, result: AgentResult, policy: ModePolicy,
                              ctx: AgentContext, state: RunState) -> AgentResult:
        agent = self.d.agents.get(step.agent)
        rules = agent.spec.verification
        tests_available = bool(((ctx.project.overview if ctx.project else {}) or {}).get("tests", {}).get("command"))
        for attempt in range(policy.max_fix_iterations + 1):
            assert result.changeset_id
            feedback: list[str] = []
            if rules.syntax_check:
                syntax = await verification.verify_syntax(ctx, result.changeset_id)
                if syntax.status == "failed":
                    feedback.append(syntax.summary)
            if not feedback and rules.run_tests and policy.run_tests and tests_available:
                tests = await verification.verify_tests(ctx, phase="proposed", changeset_id=result.changeset_id)
                if tests.status == "failed":
                    failing = "\n".join(f"- {f.get('test')}: {f.get('message')}" for f in tests.data.get("failures", []))
                    feedback.append(f"Tests fail with your change:\n{failing}\n\nOutput:\n{tests.data.get('output_tail', '')[-2500:]}")
                elif tests.status == "error":
                    state.note(f"Tests could not be run: {tests.summary}")
            if not feedback or attempt >= policy.max_fix_iterations:
                break
            await ctx.status(f"Fixing issues found by verification (attempt {attempt + 1})…")
            ctx.evidence.add(EvidenceItem(kind="notice", summary=f"Fix iteration {attempt + 1} after verification failure",
                                          step=step.id, agent=step.agent))
            retry_task = agent_task.model_copy(update={"feedback": "\n\n".join(feedback)})
            retry = await agent.run(retry_task, ctx)
            if retry.status != "succeeded":
                state.note(f"Fix attempt {attempt + 1} failed: {retry.error_message}")
                break
            retry.tool_calls = result.tool_calls + retry.tool_calls
            retry.model_ids = result.model_ids + retry.model_ids
            result = retry
        if rules.run_lint and policy.lint and result.changeset_id:
            await verification.verify_lint(ctx, result.changeset_id)
        return result

    async def _absorb(self, step: PlanStep, result: AgentResult, state: RunState, ctx: AgentContext) -> None:
        state.steps[step.id] = {"agent": step.agent, "status": result.status, "summary": result.summary,
                                "output": _jsonable(result.output), "text": result.text, "models": result.model_ids,
                                "notices": result.notices, "error": result.error_message,
                                "tools": [c.model_dump() for c in result.tool_calls]}
        for notice in result.notices:
            state.note(notice)
        for model in result.model_ids:
            if model not in state.models:
                state.models.append(model)
        if step.agent not in state.agents:
            state.agents.append(step.agent)
        if step.agent == "project_context" and result.status == "succeeded":
            state.context = result.output.get("context", "")
            state.citations = result.output.get("citations", [])
            ctx.evidence.add(EvidenceItem(kind="retrieval", status="info", summary=result.summary, step=step.id,
                                          agent=step.agent, data=result.output.get("retrieval", {})))
        elif step.agent == "debugging" and result.output.get("citations"):
            state.citations = result.output["citations"]
        if result.changeset_id:
            state.changeset_id = result.changeset_id
            ctx.evidence.add(EvidenceItem(kind="changeset", status="info", summary=f"Changes staged by {step.agent}",
                                          step=step.id, agent=step.agent, ref=result.changeset_id,
                                          data={"files": result.output.get("files", [])}))
        if result.approval_ids:
            state.pending.extend(a for a in result.approval_ids if a not in state.pending)
        if step.agent != "project_context" and result.status in ("succeeded", "needs_approval"):
            state.primary_step = step.id  # last substantive step is the primary result
            state.producer_models = list(dict.fromkeys(state.producer_models + result.model_ids))

    async def _critic(self, task: Task, intent: IntentResult, ctx: AgentContext, state: RunState, plan: Plan) -> None:
        if not state.primary_step or not self.d.agents.has("critic"):
            return
        primary_agent = state.steps[state.primary_step]["agent"]
        if not self.d.agents.spec(primary_agent).verification.critic:
            return
        outputs = {k: {**(v.get("output") or {}), **({"answer": v["text"]} if v.get("text") and
                                                    primary_agent in ("rag", "assistant", "research") else {})}
                   for k, v in state.steps.items()}
        inputs = critic_inputs(outputs)
        inputs.update({"producer_models": state.producer_models, "success_criteria": plan.success_criteria,
                       "require_citations": self.d.agents.spec(primary_agent).verification.require_citations})
        critic = self.d.agents.get("critic")
        result = await critic.run(AgentTask(step_id="critic", goal="Verify the result", request=task.request,
                                            intent=intent.intent, complexity=intent.complexity, inputs=inputs,
                                            context=state.context, citations=state.citations), ctx)
        await self._record_step(ctx.run_id, PlanStep(id="critic", agent="critic", goal="Verify the result"),  # type: ignore[arg-type]
                                result, status=result.status)
        if result.status == "succeeded":
            state.critic = result.output
        else:
            state.note(f"Verification by the critic was not possible: {result.error_message}")
        for model in result.model_ids:
            if model not in state.models:
                state.models.append(model)
        if "critic" not in state.agents:
            state.agents.append("critic")

    async def _changeset_has_files(self, changeset_id: str) -> bool:
        async with self.d.sessions() as session:
            cs = await session.get(ChangeSet, uuid.UUID(changeset_id))
            return bool(cs and cs.status == "open" and (cs.stats or {}).get("files"))

    async def _changeset_view(self, changeset_id: str | None) -> dict[str, Any] | None:
        if not changeset_id:
            return None
        async with self.d.sessions() as session:
            cs = await session.get(ChangeSet, uuid.UUID(changeset_id))
            if cs is None:
                return None
            return {"id": str(cs.id), "status": cs.status, "stats": cs.stats, "title": cs.title}

    async def _approvals_view(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        async with self.d.sessions() as session:
            rows = (await session.execute(select(Approval).where(Approval.id.in_([uuid.UUID(i) for i in ids])))).scalars()
            return [{"id": str(a.id), "title": a.title, "status": a.status, "tool": a.tool_name,
                     "risk_level": a.risk_level, "description": a.description[:1000]} for a in rows]

    async def _pause_for_approval(self, run: TaskRun, task: Task, ctx: AgentContext, state: RunState) -> None:
        approvals = await self._approvals_view(state.pending)
        text, report = await self._report(task, state, approval=approvals[0] if approvals else None)
        report["approvals"] = approvals
        await self._save(run.id, state, status=TaskStatus.AWAITING_APPROVAL.value, result=_jsonable(report))
        await self._set_task(task.id, status=TaskStatus.AWAITING_APPROVAL.value)
        await self._post_message(task, run.id, text, report, state)
        await ctx.emit("answer", text=text, report=_jsonable(report))
        for approval in approvals:
            await ctx.emit("approval_required", approval_id=approval["id"], title=approval["title"],
                           risk_level=approval["risk_level"], tool=approval["tool"], changeset_id=state.changeset_id)
        await ctx.emit("run_paused", status=TaskStatus.AWAITING_APPROVAL.value,
                       approval_ids=[a["id"] for a in approvals])

    async def _report(self, task: Task, state: RunState, *, approval: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        primary = state.steps.get(state.primary_step or "", {})
        text, report = compose_report(
            request=task.request, primary=primary, ledger=state.evidence, citations=state.citations,
            critic=state.critic, changeset=await self._changeset_view(state.changeset_id), notices=state.notices,
            approval=approval,
        )
        report.update({"intent": state.intent, "mode": state.mode, "agents": state.agents, "models": state.models,
                       "plan": state.plan.get("steps", []), "primary_agent": primary.get("agent")})
        if not text.strip() or not (primary.get("text") or primary.get("summary") or primary.get("output")):
            raise OrchestratorError("The agents finished but produced no answer.", code=ErrorCode.ORCHESTRATOR_EMPTY_RESPONSE,
                                    hint="Check the Models page — the model may be returning empty output.")
        return text, report

    async def _post_message(self, task: Task, run_id: uuid.UUID, text: str, report: dict[str, Any],
                            state: RunState) -> None:
        if not task.conversation_id:
            return
        async with self.d.sessions() as session:
            session.add(Message(organization_id=task.organization_id, conversation_id=task.conversation_id,
                                role="assistant", content=text, run_id=run_id, created_at=utcnow(),
                                meta={"report": _jsonable(report), "agents": state.agents, "models": state.models,
                                      "status": "awaiting_approval" if state.pending else "final"}))
            await session.commit()

    async def _finish(self, run: TaskRun, task: Task, ctx: AgentContext, state: RunState) -> None:
        await ctx.status("Preparing the final answer…")
        text, report = await self._report(task, state, approval=None)
        await self._save(run.id, state, status=TaskStatus.SUCCEEDED.value, result=_jsonable(report),
                         finished_at=utcnow())
        await self._set_task(task.id, status=TaskStatus.SUCCEEDED.value, finished_at=utcnow())
        await self._post_message(task, run.id, text, report, state)
        await ctx.emit("answer", text=text, report=_jsonable(report))
        await ctx.emit("run_finished", status=TaskStatus.SUCCEEDED.value)
        await self._remember(task, state)

    async def _remember(self, task: Task, state: RunState) -> None:
        if not task.project_id:
            return
        applied = state.evidence.latest("apply")
        tests_after = state.evidence.tests_passed(applied=True)
        if applied is not None and applied.status == "passed":
            summary = state.steps.get(state.primary_step or "", {}).get("summary", "")
            files = ", ".join(applied.data.get("files", [])[:10])
            await self.d.memory.remember(
                org_id=task.organization_id, scope="project", kind="fix", project_id=task.project_id,
                content=f"Change applied for '{task.request[:200]}': {summary[:500]} (files: {files}; "
                        f"tests after apply: {'passed' if tests_after else 'not passing' if tests_after is False else 'not run'})",
                source="system", importance=2,
            )
        await self.d.memory.remember(
            org_id=task.organization_id, scope="task", kind="summary", project_id=task.project_id, task_id=task.id,
            user_id=task.user_id, source="system",
            content=f"Task '{task.request[:200]}' ({state.intent.get('intent')}) handled by {', '.join(state.agents)}.",
        )

    # ------------------------------------------------------------------------------------------
    # resume after an approval decision
    # ------------------------------------------------------------------------------------------
    async def _resume(self, run_id: uuid.UUID, approval_id: uuid.UUID) -> None:
        events = self._emitter(run_id)
        run, task, project, role = await self._load(run_id)
        if run.status != TaskStatus.AWAITING_APPROVAL.value:
            log.info("orchestrator.skip_resume", status=run.status)
            return
        bind_contextvars(task_id=str(task.id), user_id=str(task.user_id), org_id=str(task.organization_id))
        state = RunState(run.state)
        await self._save(run_id, state, status=TaskStatus.RUNNING.value)
        await self._set_task(task.id, status=TaskStatus.RUNNING.value)
        ctx = self._context(run, task, project, role, events, state, state.mode)
        try:
            await self._apply_decision(approval_id, task, ctx, state)
            state.pending = [p for p in state.pending if p != str(approval_id)]
            still_pending = [a for a in await self._approvals_view(state.pending) if a["status"] == "pending"]
            if still_pending:
                state.pending = [a["id"] for a in still_pending]
                await self._pause_for_approval(run, task, ctx, state)
                return
            state.pending = []
            await self._finish(run, task, ctx, state)
        except AppError as exc:
            await self._save(run_id, state)
            await self._fail(run_id, exc.code, exc.message, hint=exc.hint)
        except Exception:
            log.exception("orchestrator.resume_crashed")
            await self._save(run_id, state)
            await self._fail(run_id, ErrorCode.ORCHESTRATOR_ERROR,
                             "An internal error interrupted the task after your decision. Details were logged.")

    async def _apply_decision(self, approval_id: uuid.UUID, task: Task, ctx: AgentContext, state: RunState) -> None:
        async with self.d.sessions() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None or approval.run_id != ctx.run_id:
                raise OrchestratorError("Approval not found for this run.", code=ErrorCode.NOT_FOUND)
            decision, tool_name, args = approval.status, approval.tool_name, dict(approval.args)
            decided_by = approval.decided_by
        await ctx.emit("approval_resolved", approval_id=str(approval_id), status=decision)
        if decision == ApprovalStatus.REJECTED.value:
            ctx.evidence.add(EvidenceItem(kind="approval", status="skipped", ref=str(approval_id),
                                          summary=f"You rejected: {tool_name}. Nothing was changed."))
            state.note("You rejected the proposed action, so nothing was changed.")
            if tool_name == "apply_changeset" and state.changeset_id:
                async with self.d.sessions() as session:
                    await session.execute(update(ChangeSet).where(ChangeSet.id == uuid.UUID(state.changeset_id))
                                          .values(status="discarded"))
                    await session.commit()
            return
        if decision != ApprovalStatus.APPROVED.value:
            raise OrchestratorError(f"Approval is '{decision}', not approved.", code=ErrorCode.CONFLICT)
        await ctx.status("Applying the approved action…")
        tool_ctx = ctx.tool_context(agent_id="orchestrator", step_key="approved_action")
        tool_ctx.approved = True
        tool_ctx.approval_id = approval_id
        tool_ctx.user_id = decided_by or ctx.user_id
        outcome = await ctx.tools.invoke(tool_name or "", args, tool_ctx)
        async with self.d.sessions() as session:
            await session.execute(update(Approval).where(Approval.id == approval_id).values(
                status=ApprovalStatus.EXECUTED.value if outcome.ok else ApprovalStatus.FAILED.value,
                result={"status": outcome.status, "summary": outcome.summary, "tool_run_id": outcome.tool_run_id}))
            await session.commit()
        ctx.evidence.add(EvidenceItem(kind="apply" if tool_name == "apply_changeset" else "tool_run",
                                      status="passed" if outcome.ok else "failed", ref=outcome.tool_run_id,
                                      summary=f"{tool_name}: {outcome.summary}",
                                      data={"files": outcome.data.get("applied_files", []), "tool": tool_name}))
        if not outcome.ok:
            state.note(f"The approved action failed: {outcome.summary}")
            return
        if tool_name == "apply_changeset":
            tests_available = bool(((ctx.project.overview if ctx.project else {}) or {}).get("tests", {}).get("command"))
            if tests_available:
                await verification.verify_tests(ctx, phase="applied", changeset_id=state.changeset_id)
        elif state.primary_step and outcome.data:
            step = state.steps[state.primary_step]
            step["text"] = (step.get("text") or "") + f"\n\n**Executed after approval:** {outcome.summary}"


def _jsonable(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, default=str))
