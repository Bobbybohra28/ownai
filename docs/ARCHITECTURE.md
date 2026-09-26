# OwnAI — Architecture Proposal (v0.1, for approval)

Private, local-first, multi-model, multi-agent AI developer workspace.

Status: **proposal — no implementation yet.** Decisions marked **[DECISION]** are
proposed defaults; items marked **[OPEN]** need your input before or during Phase 1.

---

## 0. Reading of the specification

The spec describes four products stacked on each other:

1. **A model platform** — registry of local model endpoints, health checks, routing,
   fallback, benchmarking.
2. **An agent runtime** — planner, orchestrator, ~40 specialised agents, tools,
   sandbox, approvals, verification loop.
3. **A project knowledge system** — indexing, symbol extraction, RAG, memory.
4. **A commercial SaaS / on-prem shell** — auth, tenants, plans, quotas, audit, admin UI.

The main architectural consequences:

- **Agents must be data, not code.** 40 agents written as 40 bespoke classes would be
  40 half-finished things. Most agents differ only in *role prompt, allowed tools,
  required model capabilities, output schema and verification policy*. So an agent is
  a declarative **AgentSpec** (YAML) executed by a small number of generic runtimes.
  Only agents with genuinely different control flow get code (Planner, Orchestrator,
  Critic, Project Context, Memory). Adding/removing an agent = adding/removing a spec.
- **The Model Router is deterministic code, not an LLM.** Routing must be fast,
  explainable and testable. An LLM is used only for *intent classification* (and only
  when cheap rules are not confident).
- **Every agent run is a long-running, resumable job**, because it may pause for
  human approval, run tests for minutes, or lose a model mid-run. The API process must
  not hold this state in memory.
- **"Tested" and "verified" are facts from the tool ledger, not LLM text.** The UI
  renders test results from recorded `tool_runs`; the final answer is assembled from an
  evidence record so the LLM cannot claim a test it did not run.
- **Repository and document content is untrusted input** (prompt-injection vector).
  Permissions never derive from model output; risky actions always go through an
  approval gate enforced in the tool layer.

---

## 1. Missing technical decisions (and proposed answers)

| # | Gap in the spec | Proposal |
|---|---|---|
| 1 | Job execution model for long agent runs | **[DECISION]** Separate `worker` process. Queue on **Redis Streams** (consumer groups, ack, retry) behind a `TaskQueue` interface. Redis also carries the per-run event stream used by SSE (replayable via `Last-Event-ID`). |
| 2 | Orchestration framework (LangGraph/CrewAI/own) | **[DECISION]** Own small orchestrator: a typed plan DAG + persisted step state machine. Avoids a heavy framework whose abstractions fight checkpoint/approval/audit requirements. ~1–2k LOC we fully control. |
| 3 | How tool calling works with local models | **[DECISION]** Three mechanisms selected per model capability flag: (a) native OpenAI `tools` (vLLM `--enable-auto-tool-choice` + model-specific parser), (b) **constrained JSON** via `response_format: json_schema` / vLLM guided decoding, (c) validated JSON-in-text with one repair retry. Planner/critic always use (b). |
| 4 | Reasoning-model "thinking" output | **[DECISION]** Use vLLM reasoning parsers; `reasoning_content` is kept out of responses, not stored by default (optional encrypted debug retention for admins). UI shows step summaries only. |
| 5 | Embedding + reranker serving | **[DECISION]** Any OpenAI-compatible `/v1/embeddings` (vLLM `--task embed`, TEI, llama.cpp). Reranker via `/rerank` or `/score` (vLLM, TEI). Both optional: without reranker, retrieval falls back to hybrid score fusion. |
| 6 | Retrieval strategy | **[DECISION]** Qdrant **hybrid**: dense vectors + sparse (BM25-style) vectors, fused with RRF, then optional cross-encoder rerank. Code needs lexical match on identifiers; dense-only retrieval is weak on code. |
| 7 | Code parsing | **[DECISION]** **tree-sitter** for language detection fallback, symbol extraction and AST-aware chunking (functions/classes as chunk boundaries). Line-window chunking fallback for unsupported languages. |
| 8 | Document parsing libs | **[DECISION]** `pypdf` (BSD) and `python-docx` (MIT). **Avoid PyMuPDF** — AGPL is a commercial-licensing risk. |
| 9 | Where AI file edits land | **[DECISION]** Never directly on the user's working tree. Edits go into a **ChangeSet** in a per-run git worktree/branch (`ownai/run-<id>`). User reviews the diff and applies. Deletes/pushes need approval. |
| 10 | Sandbox technology | **[DECISION]** Dedicated **sandbox-runner** service (the only component with container-runtime access). Docker/Podman rootless containers: no network by default, read-only rootfs, non-root, `cap-drop ALL`, seccomp, pids/mem/cpu limits, tmpfs scratch. **gVisor (`runsc`)** recommended for SaaS; Kubernetes Jobs + gVisor/Kata at scale. |
| 11 | SQL safety | **[DECISION]** Statement classification with **sqlglot** (read / DML / DDL / admin). Read-only transaction + `statement_timeout` + row cap by default. DML/DDL requires approval. Customer DB credentials stored envelope-encrypted, never placed in prompts. |
| 12 | Auth | **[DECISION]** Email+password (argon2id), short-lived JWT access token + rotating opaque refresh token (httpOnly cookie), API keys for programmatic use. OIDC SSO in Business plan (Phase 7). |
| 13 | Multi-tenancy | **[DECISION]** `organization_id` on every tenant-owned row from day one; enforced in the repository layer (Postgres RLS added in Phase 7). A personal user gets a personal org. Qdrant: shared collections with tenant payload index (`is_tenant`). |
| 14 | Streaming transport | **[DECISION]** **SSE** for chat/run events (proxy-friendly, resumable). Approvals and cancellation via normal REST calls. WebSocket only later if we add a live terminal. Frontend uses `fetch` streaming (native `EventSource` cannot send auth headers). |
| 15 | Token counting / context budget | **[DECISION]** Use the serving endpoint's `/tokenize` when available; fallback to a calibrated chars-per-token estimate per model (recorded in registry). |
| 16 | GPU/VRAM awareness | **[DECISION]** Read vLLM `/metrics` (KV-cache usage, running/waiting requests) as the load signal; VRAM from an optional DCGM/`nvidia-smi` exporter. vLLM does not hot-swap models, so "available VRAM" means *load headroom on already-served models*, not dynamic loading (dynamic loading is a Phase 6+ option). |
| 17 | Frontend stack | **[DECISION]** React + TypeScript + Vite, React Router, TanStack Query, Tailwind + shadcn/ui (Radix), Monaco editor, Zustand for local UI state. |
| 18 | Observability | **[DECISION]** `structlog` JSON logs with contextvars (request/user/org/project/run/agent/model/tool IDs), OpenTelemetry traces, Prometheus metrics. |
| 19 | Private-deployment licensing | **[DECISION]** Signed offline license file (Ed25519) → entitlements. No phone-home required. |
| 20 | Model licensing | **[OPEN / risk]** Model licenses differ (some restrict commercial use or user counts). The registry records a `license` field; admins must confirm. We never ship weights. |

### Questions for you **[OPEN]**

1. **Target hardware for the first deployment?** (e.g. 1× 24 GB GPU vs 2× 80 GB). This
   decides which model roles can realistically run concurrently in the default config.
2. **First go-to-market: self-hosted single-tenant, or hosted SaaS?** Architecture
   supports both; it changes Phase 1–4 priorities (e.g. gVisor/quotas earlier for SaaS).
3. **Initial language focus for deep indexing** — proposal: Python, TypeScript/JavaScript,
   Go, Java, SQL, Markdown. Others get generic chunking.
4. **Optional external providers** (OpenAI/Anthropic-compatible) allowed as an opt-in
   provider type, off by default? Proposal: yes, disabled unless the admin enables it
   per org — never required.
5. **Project import sources** for MVP: zip upload + git clone URL (proposal) — or also a
   local-path mount for on-prem?

---

## 2. Final architecture

```mermaid
flowchart LR
  UI[React Web App] -->|REST + SSE| API[FastAPI API\nstateless]
  API --> PG[(PostgreSQL\nmetadata, audit)]
  API --> RDS[(Redis\nqueue, events, rate limits)]
  API --> QD[(Qdrant\nvectors)]
  RDS --> W[Agent Worker(s)\norchestrator + agents]
  W --> PG
  W --> QD
  W --> MR[Model Router\n(in-process lib)]
  MR --> V1[vLLM: fast]
  MR --> V2[vLLM: coding]
  MR --> V3[vLLM: reasoning]
  MR --> V4[vLLM: vision]
  MR --> E1[Embeddings / Reranker]
  W -->|internal authenticated API| SB[Sandbox Runner]
  SB --> C1[[Ephemeral container\nno network, limits]]
  IX[Indexer Worker] --> QD
  IX --> PG
  RDS --> IX
  W --> FS[(Project storage\nFS volume or S3/MinIO)]
  IX --> FS
  SB --> FS
```

### Deployable services

| Service | Role | Scales |
|---|---|---|
| `api` | Auth, REST, SSE fan-out from Redis, enqueue jobs. No long work, no Docker socket. | Horizontally, stateless |
| `worker` | Orchestrator + agents + tool execution (except sandboxed execution). | Horizontally; concurrency per worker configurable |
| `indexer` | Project scanning, parsing, chunking, embedding, upsert. Same image as worker, different queue. | Horizontally |
| `sandbox-runner` | Runs code/tests/linters/docker builds in isolated containers. Only service with container runtime access. Internal network only, mTLS/shared-secret auth. | Per node |
| `postgres`, `redis`, `qdrant` | State | Standard HA options |
| `vllm-*` | Model servers (external to our code, configured via registry) | Per model, replicas |
| `web` | Static React build behind the reverse proxy | CDN / nginx |

Single-node private deployment = one `docker compose` file with all of the above.
Scaled deployment = Helm chart (Phase 7).

### Request flow (complex request)

```
POST /conversations/{id}/messages
 → api: authz, quota check, persist message, create task + task_run, enqueue, return run_id
 → client opens GET /runs/{run_id}/events (SSE)
worker:
 1. Intent detection      (rules → fast model w/ JSON schema if uncertain)
 2. Mode resolution       (Quick | Developer | Deep | Project; user choice or auto)
 3. Context gathering     (Project Context Agent: retrieval, symbols, memory)
 4. Planning              (Planner → validated Plan DAG; skipped in Quick mode)
 5. Execution             (Orchestrator runs steps; each step: Router picks model,
                           agent runs tool loop; risky tool → approval pause)
 6. Verification          (Critic + test loop per verification policy)
 7. Final response        (Responder assembles answer from evidence ledger)
 → every stage emits typed events → Redis stream → SSE → UI
```

---

## 3. Folder structure

```
ownai/
├── README.md
├── .env.example
├── config/
│   ├── config.example.yaml          # app settings (non-secret)
│   ├── models.example.yaml          # model registry + routing policy
│   ├── agents/                      # one YAML AgentSpec per agent
│   │   ├── coding.yaml
│   │   ├── debugging.yaml
│   │   └── ...
│   └── plans.example.yaml           # plan tiers → entitlements/quotas
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── migrations/                  # Alembic
│   ├── app/
│   │   ├── main.py                  # FastAPI app factory
│   │   ├── worker.py                # worker entrypoint (agent + index queues)
│   │   ├── core/                    # settings, logging, errors, ids, context vars, clock
│   │   ├── api/
│   │   │   ├── deps.py              # auth, db session, current org/project
│   │   │   ├── errors.py            # exception → user-facing error mapping
│   │   │   └── v1/                  # routers: auth, users, orgs, projects, files,
│   │   │                            #   conversations, runs, approvals, models, agents,
│   │   │                            #   tools, rag, memory, git, sql, eval, admin, billing
│   │   ├── schemas/                 # Pydantic API DTOs (request/response/events)
│   │   ├── database/
│   │   │   ├── base.py              # engine, session, mixins (org scoping, timestamps)
│   │   │   ├── models/              # SQLAlchemy ORM models
│   │   │   └── repositories/        # tenant-scoped data access
│   │   ├── models/                  # LLM model layer (NOT ORM)
│   │   │   ├── registry.py          # load/validate models.yaml, hot reload
│   │   │   ├── providers/           # openai_compatible.py (vLLM/TEI/llama.cpp/...), external (opt-in)
│   │   │   ├── router.py            # constraint filter + scoring + fallback chain
│   │   │   ├── health.py            # probes, circuit breakers, metrics scraping
│   │   │   └── tokens.py            # token counting / budget
│   │   ├── orchestrator/
│   │   │   ├── intent.py            # intent + complexity classification
│   │   │   ├── modes.py             # Quick/Developer/Deep/Project policies
│   │   │   ├── plan.py              # Plan/Step models + DAG validation
│   │   │   ├── executor.py          # step scheduling, budgets, checkpoint/resume
│   │   │   ├── verification.py      # generate→test→fix loop policy
│   │   │   ├── evidence.py          # evidence ledger (what actually happened)
│   │   │   └── events.py            # typed run events → Redis stream
│   │   ├── agents/
│   │   │   ├── base.py              # Agent protocol, AgentContext, AgentResult
│   │   │   ├── spec.py              # AgentSpec schema + loader
│   │   │   ├── registry.py
│   │   │   ├── runtimes/            # tool_loop.py (generic ReAct-style), single_shot.py
│   │   │   └── builtin/             # planner.py, critic.py, project_context.py, memory.py, responder.py
│   │   ├── tools/
│   │   │   ├── base.py              # Tool protocol, ToolSpec, permission levels
│   │   │   ├── registry.py
│   │   │   ├── executor.py          # validate → authorize → approve → run → audit
│   │   │   ├── fs.py  code_search.py  project.py  python.py  tests.py  lint.py
│   │   │   ├── sql.py  git.py  docker.py  process.py  logs.py
│   │   │   └── changeset.py         # staged edits / diffs
│   │   ├── rag/
│   │   │   ├── parsers/             # code (tree-sitter), markdown, text, pdf, docx, sql schema
│   │   │   ├── chunking.py
│   │   │   ├── embeddings.py
│   │   │   ├── vector_store.py      # VectorStore protocol + qdrant implementation
│   │   │   ├── retrieval.py         # hybrid search, filters, RRF
│   │   │   ├── rerank.py
│   │   │   └── context.py           # budgeted context assembly + citations
│   │   ├── projects/
│   │   │   ├── importer.py          # zip upload, git clone
│   │   │   ├── scanner.py           # walk, ignore rules, language detection
│   │   │   ├── analyzers/           # dependencies, frameworks, env keys, APIs, db schema, tests
│   │   │   ├── symbols.py
│   │   │   ├── graph.py             # file/symbol/dependency graph (Postgres-backed)
│   │   │   └── indexing.py          # pipeline orchestration, incremental re-index
│   │   ├── memory/                  # user / project / session memory services
│   │   ├── security/
│   │   │   ├── auth.py  passwords.py  tokens.py  rbac.py
│   │   │   ├── secrets.py           # detection + masking
│   │   │   ├── crypto.py            # envelope encryption for stored credentials
│   │   │   ├── commands.py          # command allowlist / dangerous pattern detection
│   │   │   ├── paths.py             # path jail
│   │   │   └── audit.py
│   │   ├── sandbox/                 # client for sandbox-runner
│   │   ├── evaluation/              # benchmark runner, scorers, datasets loader
│   │   ├── billing/                 # entitlements, quotas, payment provider adapter (optional)
│   │   └── services/                # application services composing the above
│   └── tests/
│       ├── unit/  integration/  e2e/
│       └── fakes/                   # fake model server (test double, never used at runtime)
├── sandbox_runner/                  # small separate FastAPI service + container images
│   ├── app/
│   └── images/                      # python, node, go runner images (pinned, minimal)
├── frontend/
│   ├── package.json  vite.config.ts
│   └── src/
│       ├── app/                     # router, providers, layout
│       ├── api/                     # typed client (generated from OpenAPI) + SSE client
│       ├── features/                # dashboard, projects, chat, code, debug, sql, documents,
│       │                            #   agents, models, tasks, git, settings, admin
│       ├── components/              # shared UI
│       └── lib/
├── evaluation/datasets/             # benchmark task sets (coding, sql, rag, tool-calling)
├── deploy/
│   ├── docker-compose.yml           # full single-node stack
│   ├── docker-compose.gpu.yml       # vLLM services overlay
│   ├── nginx/
│   └── helm/                        # Phase 7
└── docs/
```

---

## 4. Core module responsibilities

| Module | Responsibility | Must NOT |
|---|---|---|
| `core` | Settings (pydantic-settings; env > config.yaml > defaults), structured logging, error hierarchy, request context | Import from feature modules |
| `api` | HTTP concerns: validation, authn/z dependencies, error mapping, SSE streaming | Contain business logic or call models directly |
| `schemas` | Public DTOs and event types (the API contract) | Leak ORM objects |
| `database` | ORM models, sessions, tenant-scoped repositories, migrations | Contain business rules |
| `models` | Model registry, provider clients, health, routing, token budgets, usage recording | Know about agents or prompts |
| `orchestrator` | Intent, modes, planning contract, DAG execution, budgets, checkpoint/resume, verification loop, evidence ledger, events | Execute tools directly (goes through `tools.executor`) |
| `agents` | AgentSpec loading, generic runtimes, built-in special agents | Choose a concrete model (asks router with requirements) |
| `tools` | Tool definitions + the single enforcement point for validation, permission, approval, timeout, audit | Be callable except through `ToolExecutor` |
| `rag` | Parse, chunk, embed, store, retrieve, rerank, assemble cited context | Know about conversations |
| `projects` | Import, scan, analyse, symbol/dependency graph, incremental indexing | Send whole projects to LLMs |
| `memory` | User/project/session memory CRUD, extraction proposals, retrieval, secret filtering | Store secrets |
| `security` | Auth, RBAC, secret detection/masking, crypto, command/path policy, audit writer | — |
| `sandbox` | Client for sandbox-runner; typed execution requests/results | Run subprocesses on the worker host |
| `evaluation` | Benchmark datasets, runners, scorers, aggregation into routing stats | Modify routing without admin action |
| `billing` | Plans → entitlements, quota counters, optional payment adapter, license verification | Be required for local/private operation |
| `services` | Use-case orchestration (e.g. `ProjectService.import_from_git`) | Duplicate repository logic |

Dependency rule: `api → services → (orchestrator, agents, tools, rag, projects, memory, models) → database/core`. Enforced with `import-linter` in CI.

---

## 5. Agent interface

### 5.1 Contract

```python
class Agent(Protocol):
    spec: AgentSpec

    async def run(self, task: AgentTask, ctx: AgentContext) -> AgentResult: ...


class AgentSpec(BaseModel):
    id: str                                  # "coding", "sql", ...
    name: str
    description: str                         # used by Planner to select agents
    category: Literal["core", "development", "data", "devops", "security", "knowledge"]
    runtime: Literal["tool_loop", "single_shot", "builtin"]
    builtin_class: str | None = None         # for runtime == "builtin"
    system_prompt_file: str | None = None
    model_requirements: ModelRequirements    # capabilities, min_context, task_type, prefer
    allowed_tools: list[str] = []            # hard allowlist enforced by ToolExecutor
    output_schema: str | None = None         # Pydantic schema name for structured output
    max_iterations: int = 8
    max_tokens_budget: int = 32_000
    timeout_s: int = 300
    verification: VerificationPolicy = VerificationPolicy.none()
    min_plan: str = "free"                   # entitlement gate
    enabled: bool = True


class AgentContext:                          # injected; agents never construct these
    run_id: UUID; org_id: UUID; user_id: UUID; project_id: UUID | None
    router: ModelRouter
    tools: ScopedToolExecutor                # already restricted to spec.allowed_tools
    retriever: ProjectRetriever | None
    memory: MemoryView                       # read access; writes are proposals
    events: RunEventEmitter
    budget: BudgetTracker
    cancel: CancellationToken


class AgentResult(BaseModel):
    status: Literal["succeeded", "failed", "needs_input", "blocked"]
    summary: str                             # concise, user-safe (no chain-of-thought)
    output: dict | None                      # validated against output_schema
    artifacts: list[ArtifactRef]             # changesets, reports, files
    evidence: list[EvidenceRef]              # tool_run ids, retrieved chunk ids
    model_calls: list[ModelCallRef]
    error: UserFacingError | None
```

### 5.2 Agent catalogue and implementation class

| Agent | Implementation | Notes |
|---|---|---|
| Planner | builtin | Produces `Plan` via constrained JSON; validated DAG; can only reference enabled agents/tools |
| Orchestrator | orchestrator module (not an LLM agent) | Executes plan, budgets, replanning (max N) |
| Model Router | `models.router` (deterministic) | Exposed as "agent" in UI for transparency only |
| Project Context | builtin | Retrieval + symbol graph + project memory → cited context pack |
| Memory | builtin | Retrieves memories; proposes new memories after runs (user-confirmable) |
| Critic/Verifier | builtin | Independent model where available; checks against requirements + evidence |
| Responder | builtin | Assembles final answer from evidence ledger |
| Coding, Debugging, Code Review, Refactoring, Architecture, Testing, Documentation, Dependency, Migration, Git, SQL, Data Analysis, ML, RAG, Vector DB, LLM, Prompt Engineering, AI Evaluation, Docker, Kubernetes, DevOps, Cloud, API, Monitoring, Deployment, Security, Secrets Detection, Dependency Security, Research, Documentation Search | `tool_loop` spec (YAML) | Differ by prompt, tools, model requirements, verification policy |
| Permission/Sandbox | `security` + `tools.executor` (policy code) | Security decisions must not be LLM decisions |
| Vision | `single_shot` spec, requires `vision` capability | Disabled automatically when no vision model healthy |

Example spec:

```yaml
# config/agents/debugging.yaml
id: debugging
name: Debugging Agent
description: Diagnoses errors, stack traces and failing tests; proposes minimal fixes.
category: development
runtime: tool_loop
system_prompt_file: prompts/debugging.md
model_requirements:
  task_type: coding
  capabilities: [tool_calling]
  min_context: 16000
allowed_tools: [read_file, search_code, search_files, inspect_project, run_tests,
                inspect_logs, git_diff, edit_file]
output_schema: DebugReport
verification: { run_tests: true, critic: true, max_fix_iterations: 3 }
min_plan: free
```

### 5.3 Plan contract

```python
class PlanStep(BaseModel):
    id: str
    agent: str                       # must exist & be enabled & entitled
    goal: str
    inputs: dict[str, str] = {}      # references like "steps.s1.output.files"
    depends_on: list[str] = []
    expected_output: str
    risky: bool = False              # planner hint only; tool layer decides for real

class Plan(BaseModel):
    intent: str
    complexity: Literal["trivial", "simple", "moderate", "complex"]
    steps: list[PlanStep]            # validated: acyclic, ≤ max_steps, known agents
    success_criteria: list[str]      # the Critic checks against these
```

### 5.4 Modes

| Mode | Planner | Agents | Verification | Context |
|---|---|---|---|---|
| Quick | skipped | single agent chosen by intent | none (syntax check for code) | small retrieval if project attached |
| Developer | yes (≤5 steps) | as planned | tests when code changes; critic on changes | project retrieval |
| Deep Analysis | yes (≤12 steps) | as planned + Security/Review | full loop + independent critic | project retrieval + memory |
| Project | yes | as planned | as Developer | wide retrieval + architecture summary + graph |
| Auto (default) | chosen from intent/complexity | — | — | — |

---

## 6. Model interface and routing

### 6.1 Registry (config/models.yaml)

```yaml
endpoints:
  vllm-coder:
    provider: openai_compatible          # vLLM, llama.cpp server, TEI, LM Studio, Ollama(OAI API)
    base_url: ${VLLM_CODER_URL}
    api_key: ${VLLM_CODER_API_KEY:-}
    timeout_s: 120
    metrics_url: ${VLLM_CODER_METRICS_URL:-}

models:
  coder-main:
    endpoint: vllm-coder
    model: ${MODEL_CODER_NAME}           # never hard-coded in code
    roles: [coding, sql]
    capabilities: [chat, tool_calling, json_schema, streaming]
    context_window: 32768
    max_output_tokens: 4096
    tool_call_mode: native               # native | json_schema | text
    reasoning_parser: false
    priority: 50                         # admin-configured
    tier: pro                            # entitlement gate
    family: ${MODEL_CODER_FAMILY:-}      # used for critic independence
    license: "admin-confirmed"
    enabled: true
  embed-main:
    endpoint: vllm-embed
    model: ${MODEL_EMBED_NAME}
    roles: [embedding]
    capabilities: [embedding]
    dimensions: ${MODEL_EMBED_DIM}

routing:
  defaults:                              # role → ordered candidates (fallback chain)
    fast: [fast-main]
    coding: [coder-main, reasoning-main, fast-main]
    reasoning: [reasoning-main, coder-main]
    vision: [vision-main]
    embedding: [embed-main]
    rerank: [rerank-main]
    critic: [reasoning-main, coder-main]
  weights: { quality: 0.5, latency: 0.2, load: 0.2, priority: 0.1 }
  critic_independence: prefer_different_family
```

### 6.2 Provider interface

```python
class ModelProvider(Protocol):
    async def chat(self, req: ChatRequest) -> ChatResponse: ...
    def stream_chat(self, req: ChatRequest) -> AsyncIterator[ChatChunk]: ...
    async def embed(self, req: EmbedRequest) -> EmbedResponse: ...
    async def rerank(self, req: RerankRequest) -> RerankResponse: ...
    async def health(self) -> HealthStatus: ...           # /v1/models + tiny probe
    async def load_metrics(self) -> LoadMetrics | None: ... # KV-cache %, waiting reqs

class ChatRequest(BaseModel):
    messages: list[Message]; tools: list[ToolSchema] | None = None
    response_schema: dict | None = None; temperature: float = 0.2
    max_tokens: int | None = None; stop: list[str] | None = None

class ChatResponse(BaseModel):
    content: str; tool_calls: list[ToolCall]; finish_reason: str
    usage: Usage; latency_ms: int; model_id: str   # reasoning_content deliberately excluded
```

### 6.3 Router

```python
class ModelRequirements(BaseModel):
    task_type: Literal["fast", "coding", "reasoning", "vision", "embedding", "rerank", "critic", "sql"]
    capabilities: set[str] = set()
    min_context: int = 0
    complexity: Literal["trivial", "simple", "moderate", "complex"] = "simple"
    latency_sensitive: bool = False
    exclude_families: set[str] = set()     # critic independence

class RoutingDecision(BaseModel):
    model_id: str; reason: str; candidates_considered: list[CandidateScore]
    fallback_from: str | None; degraded: bool   # degraded → user is told
```

Algorithm:

1. **Filter (hard constraints):** enabled, entitled for org plan, healthy (circuit
   breaker closed), has capabilities, `context_window ≥ required`, not excluded family.
2. **Score:** `w_q·quality(task_type)` (from benchmark results if present, else configured
   priority) + `w_l·latency_score` (rolling p50) + `w_load·(1 − load)` (KV-cache/queue)
   + `w_p·priority`. Complexity shifts weights (trivial → latency-heavy; complex →
   quality-heavy). No "biggest model wins" rule anywhere.
3. **Execute with fallback:** on connection error / timeout / 5xx → record failure,
   open breaker after N failures, try next candidate. If the fallback lacks a capability
   tier the task preferred (e.g. coding → fast), `degraded=True` and a `model_fallback`
   event is shown to the user.
4. **If no candidate:** fail the step with a user-facing error ("No healthy coding-capable
   model is available. The configured coding endpoint is not responding.").
5. Every decision is persisted in `model_usage` (reason, tokens, latency, outcome).

Health: background prober every 15 s per endpoint (`GET /v1/models` + model presence),
passive health from real calls, circuit breaker (closed/open/half-open). The UI and
API report a model as available **only** from the latest health record.

---

## 7. Tool interface

```python
class PermissionLevel(IntEnum):
    READ = 0          # read_file, search_*, git_status/diff/log, inspect_*
    WRITE_STAGED = 1  # edit/create_file into ChangeSet (reversible)
    EXECUTE = 2       # run_python/tests/linter/formatter/docker_build in sandbox
    DESTRUCTIVE = 3   # delete_file, apply ChangeSet to working tree, DML/DDL SQL,
                      # git push/branch delete, docker_run with network, deploy
    ADMIN = 4         # credentials, security settings, infra changes

class ToolSpec(BaseModel):
    name: str; description: str
    input_model: type[BaseModel]; output_model: type[BaseModel]
    permission: PermissionLevel
    requires_approval: ApprovalRule        # always | never | predicate (e.g. SQL classifier)
    timeout_s: int
    sandboxed: bool
    idempotent: bool
    redact_output: bool = True             # secret masking before LLM sees it

class Tool(Protocol):
    spec: ToolSpec
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolOutcome: ...
```

`ToolExecutor.invoke(name, raw_args, ctx)` — the single choke point:

1. Tool exists and is in the calling agent's `allowed_tools`.
2. Validate args (Pydantic); reject unknown fields.
3. Authorize: user's project role permits this permission level; plan entitlement.
4. Policy checks: path jail (resolved realpath under project root, no symlink escape,
   deny list: `.git/` internals, `.env*` values), command allowlist, SQL classifier.
5. Approval: if required → persist `approvals` row, emit `approval_required`, checkpoint
   run, **suspend**. Resume only on explicit user decision (with optional edited args
   re-validated).
6. Execute with timeout (+ sandbox for `EXECUTE`+).
7. Mask secrets in output, truncate to budget (full output stored as artifact).
8. Persist `tool_runs` + `audit_logs`; emit `tool_started/tool_finished` events.
9. Return typed `ToolOutcome` (ok / error with user-facing message + technical detail id).

Tool list (spec §8) maps 1:1 onto files in `app/tools/`. `inspect_process` and
`inspect_logs` operate only on sandbox containers and project-registered log sources,
never on the host.

---

## 8. Database schema (PostgreSQL)

Conventions: UUIDv7 primary keys, `created_at/updated_at timestamptz`, `organization_id`
on every tenant-owned table, soft delete only where needed, JSONB for flexible payloads.

```
organizations(id, name, slug UNIQUE, plan_id, settings JSONB, created_at)
users(id, email UNIQUE, password_hash, display_name, is_active, is_superadmin,
      last_login_at, created_at)
memberships(org_id, user_id, role ENUM(owner,admin,member,viewer), PK(org_id,user_id))
refresh_tokens(id, user_id, token_hash, expires_at, revoked_at, user_agent, ip)
api_keys(id, org_id, user_id, name, prefix, key_hash, scopes TEXT[], last_used_at, revoked_at)

projects(id, org_id, name, description, source_type ENUM(upload,git,local),
         source_url, default_branch, storage_path, status ENUM(importing,indexing,ready,error),
         languages JSONB, frameworks JSONB, summary TEXT, settings JSONB, created_by)
project_members(project_id, user_id, role ENUM(owner,editor,viewer))    -- team plan
project_files(id, org_id, project_id, path, language, size_bytes, sha256,
              is_binary, is_generated, has_secrets, indexed_at, UNIQUE(project_id,path))
symbols(id, org_id, project_id, file_id, name, kind, qualified_name,
        start_line, end_line, signature, parent_symbol_id)
file_dependencies(project_id, from_file_id, to_file_id NULL, import_ref, kind)
index_jobs(id, org_id, project_id, status, stats JSONB, error, started_at, finished_at)
db_connections(id, org_id, project_id, name, dialect, host, port, database,
               username, secret_ciphertext BYTEA, read_only BOOL, created_by)

agents(id TEXT PK, version, spec JSONB, enabled, source ENUM(builtin,config,custom), updated_at)
model_endpoints(id TEXT PK, provider, base_url, enabled, config JSONB)
models(id TEXT PK, endpoint_id, model_name, roles TEXT[], capabilities TEXT[],
       context_window, priority, tier, family, enabled, config JSONB)
model_health(id, model_id, status ENUM(healthy,degraded,down), latency_ms,
             detail, checked_at)                                   -- time-series, pruned

conversations(id, org_id, user_id, project_id NULL, title, mode, archived, created_at)
messages(id, org_id, conversation_id, role ENUM(user,assistant,system_note),
         content TEXT, run_id NULL, metadata JSONB, created_at)
tasks(id, org_id, project_id, conversation_id, message_id, kind, mode, intent,
      status ENUM(queued,running,awaiting_approval,succeeded,failed,cancelled),
      plan JSONB, created_by, created_at, finished_at)
task_runs(id, org_id, task_id, attempt, status, state JSONB /*checkpoint*/,
          budget_used JSONB, error_code, error_detail_id, started_at, finished_at)
run_steps(id, run_id, step_key, agent_id, status, summary, output JSONB,
          started_at, finished_at)
run_events(id BIGSERIAL, run_id, seq, type, payload JSONB, created_at)  -- durable copy of stream
tool_runs(id, org_id, run_id, step_id, tool_name, args_redacted JSONB, status,
          exit_code, output_ref, error, duration_ms, sandbox_id, approval_id, created_at)
approvals(id, org_id, run_id, tool_run_id, action_summary, risk_level, args_redacted JSONB,
          status ENUM(pending,approved,rejected,expired), decided_by, decided_at, expires_at)
changesets(id, org_id, project_id, run_id, branch, base_commit, status
           ENUM(open,applied,discarded), diff_stats JSONB)

memories(id, org_id, scope ENUM(user,project,session), user_id NULL, project_id NULL,
         conversation_id NULL, kind, content TEXT, source ENUM(user,agent_proposed,system),
         confirmed BOOL, embedding_ref, expires_at, created_at)
documents(id, org_id, project_id NULL, title, doc_type, source_path, mime, sha256,
          status, version, metadata JSONB)
document_chunks(id, org_id, document_id NULL, project_file_id NULL, project_id,
                chunk_index, content TEXT, token_count, start_line, end_line,
                symbol_id NULL, metadata JSONB /*language, branch, doc_type*/,
                vector_id UUID, embedding_model)

model_usage(id, org_id, user_id, run_id, step_id, model_id, task_type, prompt_tokens,
            completion_tokens, latency_ms, ttft_ms, tokens_per_s, outcome,
            fallback_from, routing_reason, created_at)          -- partition by month
eval_datasets(id, name, kind ENUM(coding,sql,rag,tool_calling,hallucination), version, spec JSONB)
eval_runs(id, dataset_id, model_id, status, started_at, finished_at, config JSONB)
eval_results(id, eval_run_id, case_id, passed, score, latency_ms, tokens, detail JSONB)
model_stats(model_id, task_type, window, success_rate, p50_latency_ms, tokens_per_s,
            tool_call_success, verification_failure_rate, quality_score, updated_at)

plans(id TEXT PK, name, entitlements JSONB, quotas JSONB)
subscriptions(id, org_id, plan_id, provider, provider_ref, status, current_period_end)
usage_counters(org_id, period, metric, value, PK(org_id, period, metric))
audit_logs(id BIGSERIAL, org_id, actor_type, actor_id, action, resource_type,
           resource_id, request_id, ip, details JSONB, created_at)  -- append-only, partitioned
```

Qdrant collections: `code_chunks__{embedding_model_slug}`, `doc_chunks__{slug}`,
`memories__{slug}` — dense + sparse vectors; payload: `org_id` (tenant index),
`project_id`, `file_path`, `language`, `doc_type`, `symbol`, `branch`, `version`,
`chunk_id` (FK back to Postgres). Collection per embedding model makes model changes a
re-index, not a corruption.

---

## 9. API endpoints (`/api/v1`)

```
Auth        POST /auth/register  /auth/login  /auth/refresh  /auth/logout
            GET  /auth/me
            GET|POST|DELETE /auth/api-keys[/{id}]
Orgs        GET|POST /orgs   GET|PATCH /orgs/{id}   GET|POST|PATCH|DELETE /orgs/{id}/members
Projects    GET|POST /projects            (POST: create empty)
            POST /projects/import/upload  (multipart zip)   POST /projects/import/git
            GET|PATCH|DELETE /projects/{id}
            POST /projects/{id}/reindex   GET /projects/{id}/index-status
            GET  /projects/{id}/overview  (languages, frameworks, deps, summary)
            GET  /projects/{id}/tree      GET /projects/{id}/files?path=
            GET  /projects/{id}/symbols?q=
Changesets  GET /projects/{id}/changesets   GET /changesets/{id}/diff
            POST /changesets/{id}/apply  (approval)   POST /changesets/{id}/discard
Chat        GET|POST /conversations   GET|PATCH|DELETE /conversations/{id}
            GET  /conversations/{id}/messages
            POST /conversations/{id}/messages   → {message_id, run_id}
Runs        GET  /runs/{id}            GET /runs/{id}/steps     GET /runs/{id}/tool-runs
            GET  /runs/{id}/events     (SSE; supports Last-Event-ID)
            POST /runs/{id}/cancel
Tasks       GET  /tasks?status=&project_id=
Approvals   GET  /approvals?status=pending   POST /approvals/{id}/approve|reject
Models      GET  /models   GET /models/{id}   GET /models/health
            POST /models/{id}/probe  (admin)  PATCH /models/{id} (admin: enable, priority)
            POST /models/reload (admin: reload registry)
            GET  /models/routing/preview?task_type=&context=   (explain routing)
Agents      GET  /agents   GET /agents/{id}   PATCH /agents/{id} (admin: enable/disable)
Tools       GET  /tools    (schemas + permission levels)
RAG         POST /rag/search   {project_id, query, filters, top_k} → chunks + citations
Documents   GET|POST /projects/{id}/documents   DELETE /documents/{id}
Memory      GET|POST /memories?scope=   PATCH|DELETE /memories/{id}
            POST /memories/{id}/confirm
Git         GET  /projects/{id}/git/status|log|branches|diff
            POST /projects/{id}/git/push (approval)
SQL         GET|POST /projects/{id}/db-connections   DELETE /db-connections/{id}
            GET  /db-connections/{id}/schema   POST /db-connections/{id}/query (classified)
Eval        GET|POST /eval/datasets   POST /eval/runs   GET /eval/runs/{id}
            GET /eval/leaderboard?task_type=
Admin       GET /admin/health  /admin/usage  /admin/audit-logs  /admin/workers
Billing     GET /billing/plan   GET /billing/usage   POST /billing/checkout (adapter, optional)
            POST /billing/webhook/{provider}
System      GET /healthz (liveness)   GET /readyz (db, redis, qdrant)   GET /metrics
```

SSE event types: `run_started, status (human-readable progress), intent_detected,
plan_created, step_started, step_finished, model_selected, model_fallback,
tool_started, tool_finished, approval_required, approval_resolved, test_result,
verification_result, token (answer stream), sources, error, run_finished`.

---

## 10. Frontend pages

| Page | Content |
|---|---|
| Dashboard | Recent projects/conversations, pending approvals, model health summary, usage vs plan |
| Projects | List, create, import (zip/git), index progress, overview (languages, frameworks, deps, architecture summary) |
| AI Chat | Mode selector (Auto/Quick/Developer/Deep/Project), project scope, message stream; per-answer panel: model(s) used, agents, tools, files referenced (clickable), step timeline, test results (from tool_runs), verification verdict, errors, fallback notices; inline approval cards |
| Code | File tree + Monaco viewer, symbol search, ChangeSet diff review (apply/discard) |
| Debug | Paste error/log or pick failing test → debug run; run history |
| SQL/Data | Connections, schema browser, query editor with classification badge (read/DML/DDL), AI query assistant, result grid |
| Documents | Upload/list docs, index status, RAG search with source previews |
| Agents | Catalogue (description, tools, model requirements, enabled); admin enable/disable |
| Models | Registry, live health, load, latency, benchmark scores, routing preview; admin priorities |
| Tasks | All runs with status filters; run detail (steps, tool runs, audit trail) |
| Git | Status, branches, log, diffs, AI branches, push (approval) |
| Settings | Profile, preferences/memory, API keys, org members, plan/usage, security settings (admin), audit log viewer (admin), diagnostics (admin) |

---

## 11. Security model

**Trust boundaries.** Browser ↔ API (authenticated). API/worker ↔ model servers
(internal network, optional API key). Worker ↔ sandbox-runner (internal, mTLS or HMAC).
Sandbox containers ↔ nothing (no network by default). Project content, documents, tool
output and model output are all **untrusted**.

**Controls:**

1. **AuthN/AuthZ:** argon2id; JWT (15 min) + rotating refresh (reuse detection);
   RBAC org role × project role × plan entitlement checked in API deps *and* in
   `ToolExecutor` (defense in depth). All queries tenant-scoped by repository layer.
2. **Tool permissions:** agent allowlist ∩ user role ∩ permission level; approval gate
   for `DESTRUCTIVE`/`ADMIN` and predicate-based (SQL DML/DDL, network-enabled docker,
   paths outside ChangeSet). Approval shows exact action + redacted args; expires.
3. **Sandbox:** ephemeral container per execution; project mounted read-only plus a
   writable copy-on-write overlay of the ChangeSet worktree; `--network none` (per-run
   opt-in egress allowlist for dependency installs, approval required); non-root UID;
   `--cap-drop ALL`, `no-new-privileges`, seccomp default, read-only rootfs + tmpfs;
   CPU/memory/pids/disk/time limits; output size cap; no host env vars passed in;
   gVisor runtime in SaaS.
4. **Commands:** structured argv only (no shell strings) for built-in tools; allowlist
   per tool (e.g. `run_tests` → pytest/npm test/go test/…); dangerous-pattern detector
   for any user-provided command (rm -rf, curl|sh, fork bombs, credential paths).
5. **Paths:** realpath jail to project root/worktree; symlink escape rejected; deny
   `.git/objects`, key files; `.env*` values never readable — only key names.
6. **Secrets:** detector (pattern + entropy, gitleaks-style rules) at import; flagged
   ranges redacted before chunking, embedding, prompting and logging; log processor
   masks known secret formats; stored credentials envelope-encrypted with a master key
   from env/KMS; secrets never enter memory tables.
7. **Prompt injection:** retrieved content wrapped as quoted data with provenance;
   system prompts instruct to treat it as data; but safety does **not** rely on that —
   tool policy + approvals + sandbox are the enforcement. Model output cannot change
   permissions, allowlists or approval requirements.
8. **SQL:** sqlglot classification; read-only transaction by default; `statement_timeout`;
   row limit; approval for DML/DDL; recommend customers supply read-only DB roles.
9. **Audit:** append-only `audit_logs` for auth events, approvals, tool runs, admin
   changes, model config changes; request ID correlation.
10. **Transport/platform:** TLS at reverse proxy, secure cookies, CSRF protection for
    cookie-authenticated endpoints, CORS allowlist, rate limiting (Redis), upload size
    and zip-bomb/path-traversal checks, dependency scanning in CI, pinned images.
11. **Chain-of-thought:** reasoning tokens stripped at provider layer; UI shows step
    summaries generated for the user.

---

## 12. Verification system

- **Evidence ledger** per run: every tool run (command, exit code, parsed test counts),
  every retrieved source, every changeset. Immutable once written.
- **Loop** (policy from mode + agent spec): generate → static checks (syntax/lint in
  sandbox) → run relevant tests → if failures and iterations left: feed failures back to
  generating agent → re-test → Critic review → Responder.
- **Critic** receives: original request, success criteria, diff, evidence ledger,
  retrieved API signatures from project symbols (catches hallucinated APIs). Returns
  structured verdict: `{pass|fail|uncertain, issues[{severity, category, detail}], unmet_requirements[]}`.
  Uses a different model family when configured/available; if not, the UI says so.
- **Responder rules (enforced in code):** "tests passed" text is only allowed if a
  `test_result` evidence item with pass status exists; UI badges come from evidence, not
  prose. If no tests exist or none ran, the answer states that explicitly.

---

## 13. Memory

| Layer | Storage | Written by | Read by |
|---|---|---|---|
| User | `memories(scope=user)` + vectors | User directly; agent *proposals* confirmed by user | Router of prompts: preferences, style |
| Project | `memories(scope=project)` + project summary, decisions, previous fixes; schema from analyzers | Indexer (facts), agents (proposals), users | Project Context Agent |
| Session | Conversation messages + run state; rolling summary when over budget | Orchestrator | All agents in the run |

Secret detector runs on every memory write; rejected if it contains a secret.
Memories have provenance and can be edited/deleted in Settings.

---

## 14. MVP scope

The spec's 7 phases are kept. **MVP = Phases 1–4** ("Developer Preview"): the smallest
thing a real developer would pay for — import a project, ask about it with cited
answers, get code changes as reviewable diffs that were actually tested in a sandbox.

| Phase | Deliverable (each ends with passing tests + running compose stack) |
|---|---|
| **1** | Repo scaffolding; FastAPI app, config/logging/errors; Postgres + Alembic (users, orgs, memberships, tokens, conversations, messages, models, model_health, audit_logs); auth; model registry + OpenAI-compatible provider + health checks; basic streaming chat through a single routed model; React shell with login, Chat, Models pages; docker compose; CI (ruff, mypy, pytest, eslint, tsc, vitest) |
| **2** | Router (scoring + fallback + decisions), intent/mode, Planner, Orchestrator (DAG, budgets, checkpoints, events), AgentSpec runtime; Coding, Project Context (stubbed to conversation-provided context until P3), Debugging, Critic, Responder; run timeline UI |
| **3** | Project import (zip/git), scanner, tree-sitter symbols, chunking, embeddings, Qdrant hybrid retrieval, rerank, cited context assembly, documents (md/txt/pdf/docx), project + user memory; Projects/Documents UI |
| **4** | ToolExecutor + policies + approvals (pause/resume), sandbox-runner, run_python/run_tests/lint/format, ChangeSets, git read tools, verification loop with real tests; Code/Git/Tasks UI |
| 5 | SQL (connections, classifier, schema), Data, Security (+secrets, dependency security), Documentation, DevOps/Docker agents |
| 6 | Vision, evaluation/benchmark system, measured routing weights, advanced routing |
| 7 | Billing adapter, entitlements/quotas, teams, admin dashboard, RLS, OIDC, Helm, license files, hardening |

Explicitly **out of MVP:** Kubernetes/Cloud/Deployment agents executing anything
(advisory only until Phase 5+), payments, SSO, dynamic model loading.

---

## 15. Bottlenecks

1. **GPU throughput / queueing** — multi-agent runs multiply LLM calls (a Deep run may
   be 10–30 calls). Mitigations: Quick mode skips planning; small model for
   intent/summaries; parallel independent steps; per-org concurrency limits; priority
   queues by plan; vLLM prefix caching (stable system prompts first in messages).
2. **Context window** — local models often 8–32k effective. Strict token budgets per
   agent, retrieval top-k with rerank, symbol-level snippets, rolling summaries.
3. **Indexing large repos** — embedding throughput. Incremental indexing by file hash,
   ignore rules (node_modules, build dirs, lockfiles, binaries), batching, separate
   indexer queue so chat is not starved.
4. **Sandbox cold start / dependency install** — prebuilt runner images, cached
   dependency layers per project (keyed on lockfile hash), warm pool later.
5. **Sequential latency** — step DAG runs independent steps concurrently; streaming
   status keeps UX responsive.
6. **Postgres write volume** — `run_events`, `model_usage`, `audit_logs` partitioned by
   month; batch inserts; retention policies.
7. **SSE connection count** — API is stateless; Redis stream fan-out; nginx tuned for
   long-lived connections.

## 16. Risks and failure points

| Risk | Impact | Mitigation |
|---|---|---|
| Local models unreliable at tool calling / JSON | Broken plans, loops | Constrained decoding, schema validation + repair retry, iteration caps, tool-call success tracked per model and fed into routing |
| Planner over-plans (runs many agents) | Latency, cost | Mode step caps, complexity classifier, planner can only choose enabled agents, budget tracker aborts gracefully |
| Hallucinated APIs / false "fixed" claims | Trust loss | Symbol-grounded critic, evidence-based responder, tests actually run |
| Prompt injection via repo/docs | Unauthorized actions, exfiltration | Policy enforcement outside the LLM, approvals, no network in sandbox, least-privilege tool allowlists |
| Sandbox escape | Host compromise | Separate runner service, rootless runtime, gVisor, no socket in API/worker, resource limits |
| Secret leakage into vectors/logs/prompts | Credential exposure | Detection at ingestion, masking at every egress, encrypted credential store |
| Model server outage mid-run | Failed runs | Health + breakers + fallback chain + checkpoint/resume; clear user messaging |
| Embedding model change | Vector incompatibility | Collection per embedding model, background re-index |
| Model license restrictions | Legal | License field + admin confirmation; we do not ship weights |
| Scope explosion (40 agents) | Nothing finished | Declarative specs; agents enabled only when their tools exist and are tested |
| Benchmark contamination / weak evals | Wrong routing | Versioned private datasets, per-customer eval on own tasks, routing weights admin-approved |
| Redis loss | Lost in-flight events | Events also persisted to Postgres; runs resumable from checkpoint |

## 17. Scaling

- **Single node (private/on-prem):** docker compose; 1+ GPUs with one vLLM per role or
  fewer roles sharing a model (registry lets one model serve multiple roles).
- **Horizontal app tier:** API and workers are stateless; add replicas. Work
  distribution via Redis consumer groups; separate queues (`agent`, `index`, `eval`) and
  priorities per plan.
- **Model tier:** multiple endpoints per model (replicas) — router load-balances across
  healthy replicas using vLLM load metrics; different GPUs host different roles;
  large customers can dedicate endpoints per org (registry supports org-scoped models).
- **Data tier:** Postgres with PgBouncer, read replicas for dashboards, partitioned
  high-volume tables; Qdrant sharding/replication with tenant-indexed payloads; project
  storage on S3-compatible object storage (MinIO on-prem) with local worker cache.
- **Sandbox tier:** move from Docker on node → Kubernetes Jobs with gVisor/Kata,
  node pools isolated from app nodes.
- **Multi-region/SaaS:** org-pinned region; no cross-tenant caches except prefix KV
  cache inside vLLM (tenant data never shared at app level).

---

## 18. Tech stack summary

Backend: Python 3.12, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2 (async) +
asyncpg, Alembic, httpx, redis-py, qdrant-client, tree-sitter, pypdf, python-docx,
sqlglot, structlog, OpenTelemetry, prometheus-client, argon2-cffi, PyJWT, cryptography.
Tests: pytest, pytest-asyncio, testcontainers (Postgres/Redis/Qdrant), a fake
OpenAI-compatible model server used **only** in tests.
Frontend: React 18+, TypeScript, Vite, React Router, TanStack Query, Tailwind,
shadcn/ui, Monaco, Zustand, Vitest, Playwright.
Infra: Docker Compose, nginx, vLLM (external), Qdrant, Postgres 16, Redis 7.

---

**Next step:** on approval (and answers to the open questions in §1), implement
**Phase 1** as described in §14.
