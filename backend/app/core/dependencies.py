"""Application container: builds and owns all long-lived services (API and worker share it)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import redis.asyncio as redis_async
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.registry import AgentRegistry
from app.billing.service import BillingService, load_plans
from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger
from app.database.base import create_engine, create_sessionmaker, utcnow
from app.database.models import AgentRecord, ModelRecord, ModelUsage
from app.memory.manager import MemoryManager
from app.models.health import HealthStore, InMemoryHealthStore, ModelHealthService, RedisHealthStore
from app.models.providers import create_provider
from app.models.providers.base import HealthReport, ModelProvider
from app.models.registry import ModelRegistry
from app.models.router import ModelRouter, UsageEvent
from app.orchestrator.events import EventBus, InMemoryEventBus, RedisEventBus
from app.orchestrator.executor import Orchestrator, OrchestratorDeps
from app.projects.indexing import IndexingService
from app.rag.embeddings import EmbeddingService
from app.rag.rerank import Reranker
from app.rag.retrieval import HybridRetriever
from app.rag.vector_store import QdrantVectorStore
from app.sandbox.client import SandboxClient
from app.security.audit import AuditLogger
from app.security.crypto import SecretBox
from app.services.jobs import InlineQueue, JobQueue, RedisStreamQueue
from app.tools.base import ToolServices
from app.tools.changeset import ChangeSetService
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

log = get_logger(__name__)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    redis: Any
    http: httpx.AsyncClient
    registry: ModelRegistry
    providers: dict[str, ModelProvider]
    health: ModelHealthService
    router: ModelRouter
    vector_store: QdrantVectorStore
    embeddings: EmbeddingService
    retriever: HybridRetriever
    indexing: IndexingService
    audit: AuditLogger
    secret_box: SecretBox | None
    sandbox: SandboxClient
    tool_registry: ToolRegistry
    tools: ToolExecutor
    tool_services: ToolServices
    agents: AgentRegistry
    events: EventBus
    queue: JobQueue
    memory: MemoryManager
    orchestrator: Orchestrator
    billing: BillingService
    extras: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.health.stop_background()
        if isinstance(self.queue, InlineQueue):
            await self.queue.drain()
        await self.http.aclose()
        await self.vector_store.close()
        if self.redis is not None:
            await self.redis.aclose()
        await self.engine.dispose()


def _usage_sink(sessions: async_sessionmaker[AsyncSession]):  # noqa: ANN202
    async def sink(event: UsageEvent) -> None:
        import uuid as _uuid

        def as_uuid(value: Any) -> _uuid.UUID | None:
            try:
                return _uuid.UUID(str(value)) if value else None
            except ValueError:
                return None

        async with sessions() as session:
            session.add(ModelUsage(
                organization_id=as_uuid(event.context.get("org_id")), user_id=as_uuid(event.context.get("user_id")),
                run_id=as_uuid(event.context.get("run_id")), agent_id=event.context.get("agent_id"),
                model_id=event.model_id, role=event.role, operation=event.operation,
                prompt_tokens=event.prompt_tokens, completion_tokens=event.completion_tokens,
                latency_ms=event.latency_ms, success=event.success, error_code=event.error_code,
                fallback_from=event.fallback_from, routing_reason=event.routing_reason[:2000], created_at=utcnow(),
            ))
            await session.commit()

    return sink


def _health_persister(sessions: async_sessionmaker[AsyncSession]):  # noqa: ANN202
    async def persist(report: HealthReport) -> None:
        async with sessions() as session:
            record = await session.get(ModelRecord, report.model_id)
            if record is not None:
                record.last_status = str(report.status)
                record.last_checked_at = utcnow()
                record.last_error_code = report.error_code
                record.last_error = report.error
                record.last_latency_ms = report.latency_ms
                await session.commit()

    return persist


async def sync_catalog(container: Container) -> None:
    """Persist the configured models/agents and load admin overrides from the database."""
    async with container.sessions() as session:
        for m in container.registry.all():
            stmt = insert(ModelRecord).values(
                id=m.id, provider=m.provider, endpoint=m.endpoint, model_name=m.model, roles=[str(r) for r in m.roles],
                capabilities=m.capabilities.model_dump(), context_length=m.context_length)
            stmt = stmt.on_conflict_do_update(index_elements=[ModelRecord.id], set_={
                "provider": m.provider, "endpoint": m.endpoint, "model_name": m.model,
                "roles": [str(r) for r in m.roles], "capabilities": m.capabilities.model_dump(),
                "context_length": m.context_length, "updated_at": utcnow()})
            await session.execute(stmt)
        for spec in container.agents.specs():
            stmt = insert(AgentRecord).values(id=spec.id, name=spec.name, spec=spec.model_dump(mode="json"))
            stmt = stmt.on_conflict_do_update(index_elements=[AgentRecord.id], set_={
                "name": spec.name, "spec": spec.model_dump(mode="json"), "updated_at": utcnow()})
            await session.execute(stmt)
        await container.billing.sync_plans(session)
        await session.commit()
        model_rows = (await session.execute(select(ModelRecord))).scalars().all()
        container.registry.apply_overrides({r.id: {"enabled": r.enabled_override, "priority": r.priority_override}
                                            for r in model_rows})
        agent_rows = (await session.execute(select(AgentRecord))).scalars().all()
        container.agents.apply_overrides({r.id: r.enabled_override for r in agent_rows if r.enabled_override is not None})


async def build_container(settings: Settings, *, role: str = "api") -> Container:
    problems = settings.validate_for_runtime()
    if problems:
        raise ConfigurationError("Invalid configuration: " + " ".join(problems))
    engine = create_engine(settings.database_url, pool_size=settings.database_pool_size)
    sessions = create_sessionmaker(engine)
    redis_client = (redis_async.Redis.from_url(settings.redis_url, socket_timeout=60, socket_connect_timeout=5,
                                               health_check_interval=30)
                    if settings.execution_mode == "worker" else None)
    http = httpx.AsyncClient(timeout=httpx.Timeout(settings.model_request_timeout_s, connect=10),
                             limits=httpx.Limits(max_connections=100, max_keepalive_connections=20), trust_env=False)
    registry = ModelRegistry.from_settings(settings)
    for warning in registry.warnings:
        log.warning("model_registry.warning", model_id=warning.model_id, message=warning.message)
    providers = {m.id: create_provider(m, http) for m in registry.all()}
    store: HealthStore = RedisHealthStore(redis_client) if redis_client is not None else InMemoryHealthStore()
    health = ModelHealthService(registry, providers, store, ttl_s=settings.model_health_ttl_s)
    health.on_report.append(_health_persister(sessions))
    router = ModelRouter(registry, providers, health, usage_sink=_usage_sink(sessions))
    vector_store = QdrantVectorStore(settings.qdrant_url,
                                     settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None)
    embeddings = EmbeddingService(router)
    retriever = HybridRetriever(sessions, vector_store, embeddings, Reranker(router))
    indexing = IndexingService(sessions, embeddings, vector_store, max_files=settings.max_project_files,
                               max_file_kb=settings.max_indexed_file_kb)
    audit = AuditLogger(sessions)
    key = settings.encryption_key.get_secret_value()
    secret_box = SecretBox(key) if key else None
    sandbox = SandboxClient(settings.sandbox_url, settings.sandbox_token.get_secret_value(), http,
                            default_timeout_s=settings.sandbox_default_timeout_s)
    tool_registry = ToolRegistry()
    tools = ToolExecutor(tool_registry, audit)
    tool_services = ToolServices(sessions=sessions, settings=settings, sandbox=sandbox, changesets=ChangeSetService(),
                                 retriever=retriever, secret_box=secret_box)
    agents = AgentRegistry.from_directory(settings.agents_config_dir, tool_registry)
    events: EventBus = RedisEventBus(redis_client) if redis_client is not None else InMemoryEventBus()
    queue: JobQueue = RedisStreamQueue(redis_client) if redis_client is not None else InlineQueue()
    memory = MemoryManager(sessions, router)
    orchestrator = Orchestrator(OrchestratorDeps(
        settings=settings, sessions=sessions, router=router, agents=agents, tools=tools, tool_registry=tool_registry,
        tool_services=tool_services, retriever=retriever, events=events, memory=memory,
    ))
    billing = BillingService(settings, load_plans(settings.plans_config_path))
    container = Container(
        settings=settings, engine=engine, sessions=sessions, redis=redis_client, http=http, registry=registry,
        providers=providers, health=health, router=router, vector_store=vector_store, embeddings=embeddings,
        retriever=retriever, indexing=indexing, audit=audit, secret_box=secret_box, sandbox=sandbox,
        tool_registry=tool_registry, tools=tools, tool_services=tool_services, agents=agents, events=events,
        queue=queue, memory=memory, orchestrator=orchestrator, billing=billing,
    )
    register_job_handlers(container)
    await sync_catalog(container)
    log.info("container.ready", role=role, models=len(registry.all()), agents=len(agents.specs()),
             tools=len(tool_registry.all()), execution_mode=settings.execution_mode)
    return container


def register_job_handlers(container: Container) -> None:
    import uuid

    from app.evaluation.runner import run_evaluation_job

    async def run_start(payload: dict[str, Any]) -> None:
        await container.orchestrator.start(uuid.UUID(payload["run_id"]))

    async def run_resume(payload: dict[str, Any]) -> None:
        await container.orchestrator.resume(uuid.UUID(payload["run_id"]), uuid.UUID(payload["approval_id"]))

    async def index_project(payload: dict[str, Any]) -> None:
        await container.indexing.index_project(uuid.UUID(payload["project_id"]),
                                               job_id=uuid.UUID(payload["job_id"]) if payload.get("job_id") else None,
                                               full=bool(payload.get("full")))

    async def index_document(payload: dict[str, Any]) -> None:
        from app.services.documents import process_document

        await process_document(container, uuid.UUID(payload["document_id"]))

    async def evaluation(payload: dict[str, Any]) -> None:
        await run_evaluation_job(container, uuid.UUID(payload["evaluation_id"]))

    container.queue.register("run.start", run_start)
    container.queue.register("run.resume", run_resume)
    container.queue.register("project.index", index_project)
    container.queue.register("document.index", index_document)
    container.queue.register("evaluation.run", evaluation)
