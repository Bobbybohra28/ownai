"""Catalog (models/agents), memory, evaluation, billing and audit tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPkMixin


class ModelRecord(TimestampMixin, Base):
    """Persisted view of the model registry plus admin overrides (enabled/priority)."""

    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    endpoint: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(300))
    roles: Mapped[list[Any]] = mapped_column(default=list)
    capabilities: Mapped[dict[str, Any]] = mapped_column(default=dict)
    context_length: Mapped[int] = mapped_column(Integer)
    enabled_override: Mapped[bool | None] = mapped_column(Boolean)
    priority_override: Mapped[int | None] = mapped_column(Integer)
    last_status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_checked_at: Mapped[datetime | None]
    last_error_code: Mapped[str | None] = mapped_column(String(60))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)


class ModelUsage(UUIDPkMixin, Base):
    __tablename__ = "model_usage"
    __table_args__ = (Index("ix_model_usage_model_created", "model_id", "created_at"),)

    organization_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID | None]
    run_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    agent_id: Mapped[str | None] = mapped_column(String(60))
    model_id: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(30))
    operation: Mapped[str] = mapped_column(String(20))  # chat|stream|embed|rerank|health
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(60))
    fallback_from: Mapped[str | None] = mapped_column(String(120))
    routing_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime]


class AgentRecord(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    spec: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled_override: Mapped[bool | None] = mapped_column(Boolean)


class Memory(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "memories"
    __table_args__ = (Index("ix_memories_search", "search_vector", postgresql_using="gin"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    scope: Mapped[str] = mapped_column(String(20), index=True)  # user|project|conversation|task|agent
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID | None]
    agent_id: Mapped[str | None] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(40))  # preference|decision|fact|fix|summary|style|note
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="user")  # user|agent|system
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    importance: Mapped[int] = mapped_column(Integer, default=1)
    search_vector: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True))
    expires_at: Mapped[datetime | None]


class Evaluation(UUIDPkMixin, TimestampMixin, Base):
    """One evaluation run of a dataset against a model."""

    __tablename__ = "evaluations"

    dataset: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(30))  # coding|debugging|rag|sql|tool_calling|reasoning
    model_id: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    total: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]
    finished_at: Mapped[datetime | None]


class EvaluationResult(UUIDPkMixin, Base):
    __tablename__ = "evaluation_results"

    evaluation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluations.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str] = mapped_column(String(120))
    passed: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(60))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Plan(TimestampMixin, Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    price_monthly_cents: Mapped[int | None] = mapped_column(Integer)
    entitlements: Mapped[dict[str, Any]] = mapped_column(default=dict)
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Subscription(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    provider: Mapped[str] = mapped_column(String(40), default="manual")  # manual|license|<payment provider>
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    current_period_start: Mapped[datetime]
    current_period_end: Mapped[datetime | None]
    seats: Mapped[int] = mapped_column(Integer, default=1)


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (UniqueConstraint("organization_id", "period", "metric"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    metric: Mapped[str] = mapped_column(String(50))  # requests|tokens|index_files|sandbox_seconds
    value: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime]


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_org_created", "organization_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID | None]
    actor_type: Mapped[str] = mapped_column(String(20))  # user|agent|system
    actor_id: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(40))
    resource_id: Mapped[str | None] = mapped_column(String(120))
    request_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(20), default="success")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime]
