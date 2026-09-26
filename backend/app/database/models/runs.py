from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPkMixin


class Conversation(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="New conversation")
    mode: Mapped[str] = mapped_column(String(30), default="auto")
    archived: Mapped[bool] = mapped_column(default=False)
    summary: Mapped[str] = mapped_column(Text, default="")  # rolling summary of older turns
    summary_upto_message_id: Mapped[uuid.UUID | None]


class Message(UUIDPkMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))  # user|assistant|system_note
    content: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    created_at: Mapped[datetime]


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}


class Task(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "tasks"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    message_id: Mapped[uuid.UUID | None]
    request: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(30), default="auto")
    intent: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.QUEUED.value, index=True)
    plan: Mapped[dict[str, Any]] = mapped_column(default=dict)
    finished_at: Mapped[datetime | None]


class TaskRun(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "task_runs"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.QUEUED.value, index=True)
    # Checkpointed orchestrator state (completed step outputs, evidence, pending actions).
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)  # final structured result
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class RunStep(UUIDPkMixin, Base):
    __tablename__ = "run_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_key"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), index=True)
    step_key: Mapped[str] = mapped_column(String(60))
    agent_id: Mapped[str] = mapped_column(String(60))
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    model_id: Mapped[str | None] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    tools_used: Mapped[list[Any]] = mapped_column(default=list)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]


class ToolRun(UUIDPkMixin, Base):
    __tablename__ = "tool_runs"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID | None]
    step_key: Mapped[str | None] = mapped_column(String(60))
    agent_id: Mapped[str | None] = mapped_column(String(60))
    tool_name: Mapped[str] = mapped_column(String(60), index=True)
    permission: Mapped[str] = mapped_column(String(20))
    args: Mapped[dict[str, Any]] = mapped_column(default=dict)  # redacted
    status: Mapped[str] = mapped_column(String(30))  # succeeded|failed|denied|approval_required|timeout
    summary: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[dict[str, Any]] = mapped_column(default=dict)  # redacted + truncated
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    approval_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class Approval(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "approvals"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), index=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(40))  # tool_call|apply_changeset
    tool_name: Mapped[str | None] = mapped_column(String(60))
    args: Mapped[dict[str, Any]] = mapped_column(default=dict)  # exact args to execute (never secrets)
    title: Mapped[str] = mapped_column(String(400))
    description: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(20), default="high")
    status: Mapped[str] = mapped_column(String(20), default=ApprovalStatus.PENDING.value, index=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    expires_at: Mapped[datetime | None]


class ChangeSet(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "changesets"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(400), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|applied|discarded|conflict
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    verification: Mapped[dict[str, Any]] = mapped_column(default=dict)
    applied_at: Mapped[datetime | None]
    applied_by: Mapped[uuid.UUID | None]


class ChangeSetFile(UUIDPkMixin, Base):
    __tablename__ = "changeset_files"
    __table_args__ = (UniqueConstraint("changeset_id", "path"),)

    changeset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("changesets.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(String(10))  # create|modify|delete
    base_sha256: Mapped[str | None] = mapped_column(String(64))  # hash of file when change was proposed
    new_content: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[str] = mapped_column(Text, default="")
    added_lines: Mapped[int] = mapped_column(Integer, default=0)
    removed_lines: Mapped[int] = mapped_column(Integer, default=0)
