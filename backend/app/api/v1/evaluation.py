from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import ContainerDep, Principal, PrincipalDep, SessionDep, project_for, requires
from app.core.exceptions import NotFoundError, ValidationFailed
from app.database.models import Evaluation, EvaluationResult, ModelRecord
from app.evaluation.runner import get_dataset, list_datasets
from app.schemas.common import EvaluationCreate
from app.security.rbac import Permission

router = APIRouter(prefix="/evaluation", tags=["evaluation"])
Evaluator = Depends(requires(Permission.EVAL_RUN))


def _view(e: Evaluation) -> dict[str, Any]:
    return {"id": str(e.id), "dataset": e.dataset, "category": e.category, "model_id": e.model_id, "status": e.status,
            "total": e.total, "passed": e.passed, "metrics": e.metrics, "error": e.error, "created_at": e.created_at,
            "finished_at": e.finished_at}


@router.get("/datasets")
async def datasets(principal: PrincipalDep) -> list[dict[str, Any]]:
    return [{"name": d.name, "category": d.category, "description": d.description, "cases": len(d.cases)}
            for d in list_datasets()]


@router.post("/runs", status_code=202)
async def start(body: EvaluationCreate, session: SessionDep, container: ContainerDep,
                principal: Principal = Evaluator) -> dict[str, Any]:
    dataset = get_dataset(body.dataset)
    if container.registry.get(body.model_id) is None:
        raise NotFoundError(f"Unknown model '{body.model_id}'.")
    params: dict[str, Any] = {}
    if dataset.category == "rag":
        if not body.project_id:
            raise ValidationFailed("RAG evaluations need a project_id (an indexed project).")
        project = await project_for(body.project_id, principal, session)
        params = {"org_id": str(principal.org_id), "project_id": str(project.id)}
    evaluation = Evaluation(dataset=dataset.name, category=dataset.category, model_id=body.model_id, status="queued",
                            metrics=params, created_by=principal.user_id)
    session.add(evaluation)
    await session.commit()
    await container.queue.enqueue("evaluation.run", {"evaluation_id": str(evaluation.id)})
    return _view(evaluation)


@router.get("/runs")
async def list_runs(principal: PrincipalDep, session: SessionDep, model_id: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Evaluation).order_by(Evaluation.created_at.desc()).limit(200)
    if model_id:
        stmt = stmt.where(Evaluation.model_id == model_id)
    return [_view(e) for e in (await session.execute(stmt)).scalars()]


@router.get("/runs/{evaluation_id}")
async def get_run(evaluation_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    evaluation = await session.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise NotFoundError("Evaluation not found.")
    results = (await session.execute(select(EvaluationResult).where(EvaluationResult.evaluation_id == evaluation_id)))
    return {**_view(evaluation), "results": [
        {"case_id": r.case_id, "passed": r.passed, "score": r.score, "latency_ms": r.latency_ms,
         "error_code": r.error_code, "detail": r.detail} for r in results.scalars()]}


@router.get("/leaderboard")
async def leaderboard(principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    """Latest completed result per (model, dataset). Only measured numbers — no 'best model' claims without data."""
    rows = (await session.execute(select(Evaluation).where(Evaluation.status == "completed")
                                  .order_by(Evaluation.finished_at.desc()))).scalars().all()
    latest: dict[tuple[str, str], Evaluation] = {}
    for e in rows:
        latest.setdefault((e.model_id, e.dataset), e)
    return [_view(e) for e in latest.values()]


@router.post("/runs/{evaluation_id}/apply-priority")
async def apply_priority(evaluation_id: uuid.UUID, session: SessionDep, container: ContainerDep,
                         principal: Principal = Depends(requires(Permission.MODELS_MANAGE))) -> dict[str, Any]:
    """Admin action: set the model's routing priority from measured accuracy (0-100)."""
    evaluation = await session.get(Evaluation, evaluation_id)
    if evaluation is None or evaluation.status != "completed":
        raise ValidationFailed("Only completed evaluations can be applied.")
    record = await session.get(ModelRecord, evaluation.model_id)
    if record is None:
        raise NotFoundError("Model not found.")
    priority = int(round(float(evaluation.metrics.get("mean_score", 0.0)) * 100))
    record.priority_override = priority
    await session.flush()
    rows = (await session.execute(select(ModelRecord))).scalars().all()
    container.registry.apply_overrides({r.id: {"enabled": r.enabled_override, "priority": r.priority_override} for r in rows})
    await container.audit.record("model.priority_from_evaluation", organization_id=principal.org_id,
                                 actor_id=principal.user_id, resource_type="model", resource_id=evaluation.model_id,
                                 details={"evaluation_id": str(evaluation_id), "priority": priority}, session=session)
    return {"model_id": evaluation.model_id, "priority": priority}
