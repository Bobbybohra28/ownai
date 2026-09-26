"""Chat/run submission, cancellation and approval decisions."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from app.core.exceptions import ConflictError, ErrorCode, NotFoundError, PermissionDenied, ValidationFailed
from app.core.logging import get_logger
from app.database.base import utcnow
from app.database.models import (
    TERMINAL_STATUSES,
    Approval,
    ApprovalStatus,
    Conversation,
    Message,
    Organization,
    OrgRole,
    Project,
    Task,
    TaskRun,
    TaskStatus,
)
from app.orchestrator.modes import Mode
from app.security.rbac import Permission, require_permission

if TYPE_CHECKING:
    from app.core.dependencies import Container

log = get_logger(__name__)
MAX_MESSAGE_CHARS = 20_000


class RunService:
    def __init__(self, container: Container) -> None:
        self.c = container

    async def submit(self, *, org: Organization, user_id: uuid.UUID, role: OrgRole, conversation: Conversation,
                     content: str, mode: str | None) -> dict[str, Any]:
        require_permission(role, Permission.CHAT)
        content = content.strip()
        if not content:
            raise ValidationFailed("Message cannot be empty.")
        if len(content) > MAX_MESSAGE_CHARS:
            raise ValidationFailed(f"Message is too long (max {MAX_MESSAGE_CHARS} characters).")
        mode_value = mode or conversation.mode or "auto"
        if mode_value not in {m.value for m in Mode}:
            raise ValidationFailed(f"Unknown mode '{mode_value}'.")
        if mode_value == Mode.DEEP.value and not self.c.billing.entitled(org, "deep_mode"):
            raise PermissionDenied("Deep Analysis mode is not included in your plan.")
        async with self.c.sessions() as session:
            await self.c.billing.check(session, org, "requests_per_month")
            active = (await session.execute(select(TaskRun.id).join(Task, Task.id == TaskRun.task_id).where(
                Task.conversation_id == conversation.id,
                TaskRun.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value])))).first()
            if active:
                raise ConflictError("A task is already running in this conversation. Wait for it or cancel it.")
            if conversation.project_id:
                project = await session.get(Project, conversation.project_id)
                if project is None or project.organization_id != org.id:
                    raise NotFoundError("The conversation's project no longer exists.")
            message = Message(organization_id=org.id, conversation_id=conversation.id, role="user", content=content,
                              created_at=utcnow())
            session.add(message)
            await session.flush()
            task = Task(organization_id=org.id, user_id=user_id, project_id=conversation.project_id,
                        conversation_id=conversation.id, message_id=message.id, request=content, mode=mode_value)
            session.add(task)
            await session.flush()
            run = TaskRun(organization_id=org.id, task_id=task.id, status=TaskStatus.QUEUED.value)
            session.add(run)
            message.run_id = run.id
            if conversation.title in ("", "New conversation"):
                await session.execute(update(Conversation).where(Conversation.id == conversation.id)
                                      .values(title=content[:80]))
            await self.c.billing.increment(session, org.id, "requests")
            await session.commit()
            ids = {"message_id": str(message.id), "task_id": str(task.id), "run_id": str(run.id)}
        await self.c.queue.enqueue("run.start", {"run_id": ids["run_id"]})
        await self.c.audit.record("run.submitted", organization_id=org.id, actor_id=user_id, resource_type="run",
                                  resource_id=ids["run_id"], details={"mode": mode_value})
        return ids

    async def cancel(self, run_id: uuid.UUID, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
        async with self.c.sessions() as session:
            run = (await session.execute(select(TaskRun).where(TaskRun.id == run_id,
                                                               TaskRun.organization_id == org_id))).scalar_one_or_none()
            if run is None:
                raise NotFoundError("Run not found.")
            if run.status in TERMINAL_STATUSES:
                return
            run.cancel_requested = True
            if run.status in (TaskStatus.QUEUED.value, TaskStatus.AWAITING_APPROVAL.value):
                run.status = TaskStatus.CANCELLED.value
                run.finished_at = utcnow()
                await session.execute(update(Task).where(Task.id == run.task_id).values(
                    status=TaskStatus.CANCELLED.value, finished_at=utcnow()))
                await session.execute(update(Approval).where(Approval.run_id == run.id,
                                                             Approval.status == ApprovalStatus.PENDING.value)
                                      .values(status=ApprovalStatus.EXPIRED.value))
            await session.commit()
        await self.c.events.publish(run_id, "run_finished", {"status": "cancelled"})
        await self.c.audit.record("run.cancelled", organization_id=org_id, actor_id=user_id, resource_type="run",
                                  resource_id=run_id)

    async def decide(self, approval_id: uuid.UUID, *, org_id: uuid.UUID, user_id: uuid.UUID, role: OrgRole,
                     approve: bool, note: str = "") -> Approval:
        require_permission(role, Permission.APPROVAL_DECIDE)
        async with self.c.sessions() as session:
            approval = (await session.execute(select(Approval).where(
                Approval.id == approval_id, Approval.organization_id == org_id).with_for_update())).scalar_one_or_none()
            if approval is None:
                raise NotFoundError("Approval not found.")
            if approval.status != ApprovalStatus.PENDING.value:
                raise ConflictError(f"This approval was already {approval.status}.")
            if approval.expires_at and approval.expires_at < utcnow():
                approval.status = ApprovalStatus.EXPIRED.value
                await session.commit()
                raise ConflictError("This approval request has expired.", code=ErrorCode.CONFLICT)
            approval.status = ApprovalStatus.APPROVED.value if approve else ApprovalStatus.REJECTED.value
            approval.decided_by = user_id
            approval.decided_at = utcnow()
            approval.decision_note = note[:2000]
            await session.commit()
            await session.refresh(approval)
        await self.c.audit.record("approval.approved" if approve else "approval.rejected", organization_id=org_id,
                                  actor_id=user_id, resource_type="approval", resource_id=approval_id,
                                  details={"tool": approval.tool_name, "title": approval.title})
        if approval.run_id:
            await self.c.queue.enqueue("run.resume", {"run_id": str(approval.run_id), "approval_id": str(approval_id)})
        return approval
