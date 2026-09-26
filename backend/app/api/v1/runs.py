"""Runs: status, steps, tool runs, cancellation and Server-Sent Events streaming."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import ContainerDep, PrincipalDep, SessionDep
from app.core.exceptions import NotFoundError
from app.database.models import TERMINAL_STATUSES, RunEvent, RunStep, Task, TaskRun, TaskStatus, ToolRun
from app.orchestrator.events import TERMINAL_EVENTS
from app.services.runs import RunService

router = APIRouter(prefix="/runs", tags=["runs"])


async def _run(session: SessionDep, run_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID) -> tuple[TaskRun, Task]:
    row = (await session.execute(select(TaskRun, Task).join(Task, Task.id == TaskRun.task_id).where(
        TaskRun.id == run_id, TaskRun.organization_id == org_id))).first()
    if row is None or row[1].user_id != user_id:
        raise NotFoundError("Run not found.")
    return row[0], row[1]


def _run_view(run: TaskRun, task: Task) -> dict[str, Any]:
    return {"id": str(run.id), "task_id": str(task.id), "status": run.status, "request": task.request,
            "intent": task.intent, "mode": task.mode, "project_id": str(task.project_id) if task.project_id else None,
            "conversation_id": str(task.conversation_id) if task.conversation_id else None,
            "result": run.result, "error_code": run.error_code, "error_message": run.error_message,
            "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
            "plan": task.plan}


@router.get("")
async def list_runs(principal: PrincipalDep, session: SessionDep, status: str | None = None,
                    project_id: uuid.UUID | None = None, limit: int = 50) -> list[dict[str, Any]]:
    stmt = (select(TaskRun, Task).join(Task, Task.id == TaskRun.task_id)
            .where(TaskRun.organization_id == principal.org_id, Task.user_id == principal.user_id))
    if status:
        stmt = stmt.where(TaskRun.status == status)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    rows = (await session.execute(stmt.order_by(TaskRun.created_at.desc()).limit(min(limit, 200)))).all()
    return [{**_run_view(r, t), "result": None} for r, t in rows]


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    run, task = await _run(session, run_id, principal.org_id, principal.user_id)
    steps = (await session.execute(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.started_at))).scalars()
    return {**_run_view(run, task), "steps": [
        {"step": s.step_key, "agent": s.agent_id, "goal": s.goal, "status": s.status, "model_id": s.model_id,
         "summary": s.summary, "error_code": s.error_code, "error_message": s.error_message, "tools": s.tools_used,
         "started_at": s.started_at, "finished_at": s.finished_at} for s in steps]}


@router.get("/{run_id}/tool-runs")
async def tool_runs(run_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    await _run(session, run_id, principal.org_id, principal.user_id)
    rows = (await session.execute(select(ToolRun).where(ToolRun.run_id == run_id).order_by(ToolRun.created_at))).scalars()
    return [{"id": str(t.id), "tool": t.tool_name, "agent": t.agent_id, "step": t.step_key, "status": t.status,
             "permission": t.permission, "summary": t.summary, "args": t.args, "output": t.output,
             "error_code": t.error_code, "duration_ms": t.duration_ms, "created_at": t.created_at} for t in rows]


@router.post("/{run_id}/cancel", status_code=202)
async def cancel(run_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep) -> dict:
    await _run(session, run_id, principal.org_id, principal.user_id)
    await session.commit()
    await RunService(container).cancel(run_id, principal.org_id, principal.user_id)
    return {"status": "cancel_requested"}


def _sse(seq: int, event_type: str, data: dict[str, Any]) -> str:
    return f"id: {seq}\nevent: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/{run_id}/events")
async def events(run_id: uuid.UUID, request: Request, principal: PrincipalDep, session: SessionDep,
                 container: ContainerDep, last_event_id: Annotated[str | None, Header()] = None,
                 after: int = 0) -> StreamingResponse:
    """Server-Sent Events for a run. Resumable via the Last-Event-ID header (or ?after=)."""
    run, _ = await _run(session, run_id, principal.org_id, principal.user_id)
    start_after = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
    initial_status = run.status
    await session.commit()

    async def stream() -> AsyncIterator[str]:
        cursor = start_after
        yield "retry: 3000\n\n"
        if initial_status in TERMINAL_STATUSES or initial_status == TaskStatus.AWAITING_APPROVAL.value:
            # finished (or paused) runs: replay persisted events from the database
            async with container.sessions() as s:
                rows = (await s.execute(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                                        .order_by(RunEvent.seq))).scalars().all()
            delivered_terminal = False
            for row in rows:
                yield _sse(row.seq, row.type, row.payload)
                delivered_terminal = delivered_terminal or row.type in TERMINAL_EVENTS
            if not delivered_terminal:
                yield _sse(cursor + 10_000, "run_finished" if initial_status in TERMINAL_STATUSES else "run_paused",
                           {"status": initial_status, "replayed": True})
            return
        idle_cycles = 0
        while True:
            if await request.is_disconnected():
                return
            batch = await container.events.read(run_id, cursor, block_ms=10_000)
            if not batch:
                idle_cycles += 1
                yield ": keep-alive\n\n"
                if idle_cycles % 3 == 0:  # every ~30s verify the run is still alive
                    async with container.sessions() as s:
                        status = (await s.execute(select(TaskRun.status).where(TaskRun.id == run_id))).scalar()
                    if status in TERMINAL_STATUSES:
                        yield _sse(cursor + 1, "run_finished", {"status": status, "detected": "status_poll"})
                        return
                continue
            idle_cycles = 0
            for event in batch:
                cursor = max(cursor, int(event["seq"]))
                yield _sse(int(event["seq"]), str(event["type"]), dict(event["data"]))
                if event["type"] in TERMINAL_EVENTS:
                    return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
