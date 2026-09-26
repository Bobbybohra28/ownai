"""Plans, entitlements, quotas and usage metering. Payment providers are pluggable and optional."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import REPO_ROOT, Settings
from app.core.exceptions import ConfigurationError, NotFoundError, QuotaExceeded
from app.database.base import utcnow
from app.database.models import Membership, Organization, Plan, Project, Subscription, UsageRecord

DEFAULT_PLANS_FILE = REPO_ROOT / "config" / "plans.example.yaml"


class PlanDef(BaseModel):
    id: str
    name: str
    price_monthly_cents: int | None = None
    limits: dict[str, int | None] = Field(default_factory=dict)
    entitlements: dict[str, Any] = Field(default_factory=dict)


def load_plans(path: Path | None) -> dict[str, PlanDef]:
    source = path or DEFAULT_PLANS_FILE
    if not source.exists():
        raise ConfigurationError(f"Plans file not found: {source}")
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return {pid: PlanDef(id=pid, **(data or {})) for pid, data in (raw.get("plans") or {}).items()}


def current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


class PaymentProvider(ABC):
    """Integration point for a payment processor (e.g. Stripe). Not required for local use."""

    name: str

    @abstractmethod
    async def create_checkout(self, org: Organization, plan: PlanDef, success_url: str, cancel_url: str) -> str: ...

    @abstractmethod
    async def handle_webhook(self, payload: bytes, signature: str | None) -> dict[str, Any]: ...


class NoPaymentProvider(PaymentProvider):
    name = "none"

    async def create_checkout(self, org: Organization, plan: PlanDef, success_url: str, cancel_url: str) -> str:
        raise ConfigurationError("No payment provider is configured on this installation. Plans are assigned by an "
                                 "administrator.")

    async def handle_webhook(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        raise ConfigurationError("No payment provider is configured.")


class BillingService:
    def __init__(self, settings: Settings, plans: dict[str, PlanDef], provider: PaymentProvider | None = None) -> None:
        self.settings = settings
        self.plans = plans
        self.provider = provider or NoPaymentProvider()
        if settings.default_plan not in plans:
            raise ConfigurationError(f"Default plan '{settings.default_plan}' is not defined in the plans file.")

    async def sync_plans(self, session: AsyncSession) -> None:
        for plan in self.plans.values():
            stmt = insert(Plan).values(id=plan.id, name=plan.name, price_monthly_cents=plan.price_monthly_cents,
                                       limits=plan.limits, entitlements=plan.entitlements, active=True)
            stmt = stmt.on_conflict_do_update(index_elements=[Plan.id], set_={
                "name": plan.name, "price_monthly_cents": plan.price_monthly_cents, "limits": plan.limits,
                "entitlements": plan.entitlements, "active": True})
            await session.execute(stmt)

    def plan_for(self, org: Organization) -> PlanDef:
        if not self.settings.billing_enabled:
            return self.plans[self.settings.default_plan]
        return self.plans.get(org.plan_id) or self.plans[self.settings.default_plan]

    async def usage(self, session: AsyncSession, org_id: uuid.UUID, period: str | None = None) -> dict[str, int]:
        rows = (await session.execute(select(UsageRecord.metric, UsageRecord.value).where(
            UsageRecord.organization_id == org_id, UsageRecord.period == (period or current_period())))).all()
        return {m: int(v) for m, v in rows}

    async def increment(self, session: AsyncSession, org_id: uuid.UUID, metric: str, amount: int = 1) -> None:
        stmt = insert(UsageRecord).values(organization_id=org_id, period=current_period(), metric=metric, value=amount,
                                          updated_at=utcnow())
        stmt = stmt.on_conflict_do_update(constraint="uq_usage_records_organization_id",
                                          set_={"value": UsageRecord.value + amount, "updated_at": utcnow()})
        await session.execute(stmt)

    async def check(self, session: AsyncSession, org: Organization, limit: str) -> None:
        plan = self.plan_for(org)
        maximum = plan.limits.get(limit)
        if maximum is None:
            return
        if limit == "requests_per_month":
            used = (await self.usage(session, org.id)).get("requests", 0)
        elif limit == "projects":
            used = int((await session.execute(select(func.count(Project.id)).where(
                Project.organization_id == org.id))).scalar_one())
        elif limit == "members":
            used = int((await session.execute(select(func.count(Membership.id)).where(
                Membership.organization_id == org.id))).scalar_one())
        else:
            return
        if used >= maximum:
            raise QuotaExceeded(f"Your {plan.name} plan allows {maximum} {limit.replace('_', ' ')}; the limit is reached.",
                                hint="Upgrade the plan or ask an administrator to raise the limit.")

    def entitled(self, org: Organization, feature: str) -> bool:
        return bool(self.plan_for(org).entitlements.get(feature))

    async def set_plan(self, session: AsyncSession, org: Organization, plan_id: str, *, provider: str = "manual") -> None:
        if plan_id not in self.plans:
            raise NotFoundError(f"Unknown plan '{plan_id}'.")
        org.plan_id = plan_id
        session.add(Subscription(organization_id=org.id, plan_id=plan_id, provider=provider,
                                 current_period_start=utcnow()))
