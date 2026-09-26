from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, requires
from app.core.exceptions import NotFoundError, PermissionDenied
from app.schemas.common import CheckoutRequest, SetPlanRequest
from app.security.rbac import Permission

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans")
async def plans(principal: PrincipalDep, container: ContainerDep) -> list[dict[str, Any]]:
    return [p.model_dump() for p in container.billing.plans.values()]


@router.get("/subscription")
async def subscription(principal: PrincipalDep, session: SessionDep, container: ContainerDep) -> dict[str, Any]:
    plan = container.billing.plan_for(principal.org)
    return {"billing_enabled": container.settings.billing_enabled, "plan": plan.model_dump(),
            "usage": await container.billing.usage(session, principal.org_id),
            "payment_provider": container.billing.provider.name}


@router.post("/plan")
async def set_plan(body: SetPlanRequest, session: SessionDep, container: ContainerDep,
                   principal: Principal = Depends(requires(Permission.BILLING_MANAGE))) -> dict[str, Any]:
    """Manual plan assignment (installation administrators / license-based deployments)."""
    if not principal.user.is_superadmin:
        raise PermissionDenied("Plans are assigned by the installation administrator or the payment provider.")
    org = await session.merge(principal.org)
    await container.billing.set_plan(session, org, body.plan_id)
    await container.audit.record("billing.plan_changed", organization_id=principal.org_id, actor_id=principal.user_id,
                                 resource_type="organization", resource_id=principal.org_id,
                                 details={"plan": body.plan_id}, session=session)
    return {"plan_id": body.plan_id}


@router.post("/checkout")
async def checkout(body: CheckoutRequest, container: ContainerDep,
                   principal: Principal = Depends(requires(Permission.BILLING_MANAGE))) -> dict[str, Any]:
    plan = container.billing.plans.get(body.plan_id)
    if plan is None:
        raise NotFoundError("Unknown plan.")
    url = await container.billing.provider.create_checkout(principal.org, plan, body.success_url, body.cancel_url)
    return {"checkout_url": url}


@router.post("/webhook/{provider}")
async def webhook(provider: str, request: Request, container: ContainerDep) -> dict[str, Any]:
    if provider != container.billing.provider.name:
        raise NotFoundError("Unknown payment provider.")
    return await container.billing.provider.handle_webhook(await request.body(), request.headers.get("stripe-signature"))
