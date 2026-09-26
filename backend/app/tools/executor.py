"""The single enforcement point for tool calls.

validate → authorize (agent allowlist, role, project) → approval gate → run with
timeout → mask/truncate output → persist ToolRun + audit log.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger
from app.database.base import utcnow
from app.database.models import Approval, ApprovalStatus, ToolRun
from app.security.audit import AuditLogger
from app.security.secrets import redact_mapping
from app.tools.base import ROLE_MAX_PERMISSION, PermissionLevel, ToolContext, ToolOutcome
from app.tools.registry import ToolRegistry

log = get_logger(__name__)

MAX_STORED_OUTPUT_CHARS = 60_000
APPROVAL_TTL = timedelta(days=7)


def _truncate_json(data: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(data, default=str)
    if len(text) <= MAX_STORED_OUTPUT_CHARS:
        return data
    return {"truncated": True, "preview": text[:MAX_STORED_OUTPUT_CHARS]}


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, audit: AuditLogger) -> None:
        self.registry = registry
        self.audit = audit

    async def invoke(self, name: str, raw_args: dict[str, Any] | None, ctx: ToolContext, *,
                     allowed: set[str] | None = None) -> ToolOutcome:
        started = time.perf_counter()
        raw_args = raw_args or {}
        if not self.registry.has(name):
            return await self._finish(ctx, name, "read", raw_args, ToolOutcome(
                status="error", error_code=str(ErrorCode.TOOL_NOT_FOUND),
                summary=f"Unknown tool '{name}'. Available: {', '.join(sorted(allowed or []))}"), started)
        tool = self.registry.get(name)
        perm = tool.permission.label
        if allowed is not None and name not in allowed:
            return await self._finish(ctx, name, perm, raw_args, ToolOutcome(
                status="denied", error_code=str(ErrorCode.TOOL_NOT_ALLOWED),
                summary=f"Tool '{name}' is not available to this agent."), started)
        if ROLE_MAX_PERMISSION.get(ctx.role, PermissionLevel.READ) < tool.permission:
            return await self._finish(ctx, name, perm, raw_args, ToolOutcome(
                status="denied", error_code=str(ErrorCode.PERMISSION_DENIED),
                summary=f"Your role ({ctx.role.value}) cannot use '{name}' ({perm})."), started)
        if tool.requires_project and ctx.project is None:
            return await self._finish(ctx, name, perm, raw_args, ToolOutcome(
                status="error", error_code=str(ErrorCode.INSUFFICIENT_CONTEXT),
                summary="This tool needs a project; none is selected."), started)
        try:
            args = tool.args_model.model_validate(raw_args)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'args'}: {e['msg']}" for e in exc.errors()[:6])
            return await self._finish(ctx, name, perm, raw_args, ToolOutcome(
                status="error", error_code=str(ErrorCode.TOOL_INVALID_ARGS),
                summary=f"Invalid arguments for {name}: {problems}"), started)

        if not ctx.approved:
            request = tool.approval_needed(args, ctx)
            if request is not None:
                approval_id = await self._create_approval(ctx, name, args.model_dump(mode="json"), request)
                return await self._finish(ctx, name, perm, raw_args, ToolOutcome(
                    status="approval_required", approval_id=str(approval_id),
                    summary=f"'{request.title}' needs your approval before it runs.",
                    data={"title": request.title, "risk_level": request.risk_level}), started,
                    approval_id=approval_id)

        try:
            outcome = await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_s)
        except TimeoutError:
            outcome = ToolOutcome(status="error", error_code=str(ErrorCode.TOOL_TIMEOUT),
                                  summary=f"{name} did not finish within {tool.timeout_s}s and was stopped.")
        except AppError as exc:
            log.info("tool.failed", tool=name, code=exc.code, error=exc.message, detail=exc.detail)
            outcome = ToolOutcome(status="denied" if exc.code in {ErrorCode.PATH_NOT_ALLOWED, ErrorCode.PERMISSION_DENIED,
                                                                   ErrorCode.COMMAND_NOT_ALLOWED} else "error",
                                  error_code=str(exc.code), summary=exc.message + (f" ({exc.hint})" if exc.hint else ""))
        except Exception as exc:
            log.exception("tool.crashed", tool=name)
            outcome = ToolOutcome(status="error", error_code=str(ErrorCode.TOOL_FAILED),
                                  summary=f"{name} failed unexpectedly ({type(exc).__name__}).")
        return await self._finish(ctx, name, perm, raw_args, outcome, started, approval_id=ctx.approval_id)

    async def _create_approval(self, ctx: ToolContext, tool_name: str, args: dict[str, Any], request: Any) -> uuid.UUID:
        async with ctx.services.sessions() as session:
            approval = Approval(
                organization_id=ctx.org_id, project_id=ctx.project.id if ctx.project else None, run_id=ctx.run_id,
                requested_by=ctx.user_id, kind="tool_call", tool_name=tool_name, args=args,
                title=request.title[:400], description=request.description, risk_level=request.risk_level,
                status=ApprovalStatus.PENDING.value, expires_at=utcnow() + APPROVAL_TTL,
            )
            session.add(approval)
            await session.commit()
            await self.audit.record("approval.requested", organization_id=ctx.org_id, actor_type="agent",
                                    actor_id=ctx.agent_id or "system", resource_type="approval",
                                    resource_id=approval.id, details={"tool": tool_name, "title": request.title})
            return approval.id

    async def _finish(self, ctx: ToolContext, name: str, permission: str, raw_args: dict[str, Any],
                      outcome: ToolOutcome, started: float, *, approval_id: uuid.UUID | None = None) -> ToolOutcome:
        duration = int((time.perf_counter() - started) * 1000)
        record = ToolRun(
            organization_id=ctx.org_id, run_id=ctx.run_id, project_id=ctx.project.id if ctx.project else None,
            user_id=ctx.user_id, step_key=ctx.step_key, agent_id=ctx.agent_id, tool_name=name, permission=permission,
            args=_truncate_json(redact_mapping(raw_args)),  # type: ignore[arg-type]
            status={"ok": "succeeded", "error": "failed"}.get(outcome.status, outcome.status),
            summary=outcome.summary[:2000], output=_truncate_json(redact_mapping(outcome.data)),  # type: ignore[arg-type]
            error_code=outcome.error_code, error_message=None if outcome.ok else outcome.summary[:2000],
            duration_ms=duration, approval_id=approval_id, created_at=utcnow(),
        )
        try:
            async with ctx.services.sessions() as session:
                session.add(record)
                await session.commit()
                outcome.tool_run_id = str(record.id)
        except Exception as exc:  # the tool result is still returned; persistence failure is logged loudly
            log.error("tool.persist_failed", tool=name, error=str(exc))
        if permission not in ("read",) or outcome.status in ("denied", "approval_required"):
            await self.audit.record(
                f"tool.{outcome.status}", organization_id=ctx.org_id, actor_type="agent" if ctx.agent_id else "user",
                actor_id=ctx.agent_id or (str(ctx.user_id) if ctx.user_id else None), resource_type="tool",
                resource_id=name, outcome="success" if outcome.ok else outcome.status,
                details={"run_id": str(ctx.run_id) if ctx.run_id else None, "tool_run_id": outcome.tool_run_id,
                         "duration_ms": duration, "summary": outcome.summary[:300]},
            )
        log.info("tool.finished", tool=name, status=outcome.status, duration_ms=duration, agent=ctx.agent_id,
                 run_id=str(ctx.run_id) if ctx.run_id else None)
        return outcome
