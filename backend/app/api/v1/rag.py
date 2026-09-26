"""RAG search (hybrid retrieval with citations) and optional cited answers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.agents.base import AgentContext, AgentTask
from app.api.deps import ContainerDep, PrincipalDep, SessionDep, project_for
from app.orchestrator.evidence import EvidenceLedger
from app.rag.context import build_context
from app.schemas.common import RAGSearchRequest
from app.tools.base import ProjectRef

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/search")
async def search(body: RAGSearchRequest, principal: PrincipalDep, session: SessionDep,
                 container: ContainerDep) -> dict[str, Any]:
    project = await project_for(body.project_id, principal, session) if body.project_id else None
    await session.commit()
    result = await container.retriever.retrieve(
        body.query, org_id=principal.org_id, project_id=project.id if project else None, top_k=body.top_k,
        filters={"language": body.language, "document_type": body.document_type, "path_prefix": body.path_prefix},
    )
    pack = build_context(result.chunks, budget_tokens=6000)
    response: dict[str, Any] = {
        "results": [{**c.to_dict(), "content": c.content[:4000]} for c in result.chunks],
        "citations": [c.to_dict() for c in pack.citations],
        "semantic": result.used_dense, "lexical": result.used_lexical, "reranked": result.used_rerank,
        "notices": result.notices,
    }
    if body.answer and pack.citations:
        ctx = AgentContext(
            run_id=None, org_id=principal.org_id, user_id=principal.user_id, role=principal.role,
            settings=container.settings, router=container.router, tools=container.tools,
            tool_registry=container.tool_registry, tool_services=container.tool_services, events=None,
            evidence=EvidenceLedger(), retriever=container.retriever,
            project=ProjectRef(project.id, project.organization_id, project.name, Path(project.storage_path),
                               project.overview or {}) if project else None,
        )
        agent = container.agents.get("rag")
        answer = await agent.run(AgentTask(step_id="answer", goal=body.query, request=body.query, intent="explain",
                                           context=pack.text, citations=[c.to_dict() for c in pack.citations]), ctx)
        response["answer"] = {"status": answer.status, "text": answer.text, "citations": answer.citations,
                              "models": answer.model_ids, "error": answer.error_message}
    return response
