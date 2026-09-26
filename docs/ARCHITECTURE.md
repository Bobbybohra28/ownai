# OwnAI — Architecture (as implemented)

OwnAI is a private, local-first, multi-model, multi-agent AI developer platform.
Backend: **Python 3.12 + FastAPI** only. Frontend: React + TypeScript (UI only).

```
Browser (React)
   │ REST + Server-Sent Events
   ▼
FastAPI API ──────────────► PostgreSQL (metadata, audit, full-text index)
   │  enqueue jobs              Qdrant (vectors)
   ▼                            Redis (job stream, run events, shared model health)
Worker(s) ─ Orchestrator ─ Agents ─ Model Router ─ Providers ─► vLLM / Ollama / OpenAI-compatible
   │                        │
   │                        └─ Tool Executor ─► Sandbox Runner ─► isolated Docker containers
   └─ Indexing (scanner → symbols → chunks → embeddings → Qdrant)
```

## Services

| Service | Code | Responsibility |
|---|---|---|
| `api` | `backend/app/main.py` | REST + SSE, auth, RBAC, enqueue jobs. Stateless. |
| `worker` | `backend/app/worker.py` | Runs agent tasks, indexing, document processing, evaluations (Redis Streams consumer group, ack, heartbeats for long jobs, re-delivery from crashed workers, dead-lettering). |
| `sandbox-runner` | `sandbox_runner/app/main.py` | The **only** component with container-runtime access. Executes code/tests/linters in throw-away containers. |
| `postgres`, `redis`, `qdrant` | — | State. |
| `web` | `frontend/` | Static React build behind nginx (SSE-friendly proxy). |

`OWNAI_EXECUTION_MODE=inline` runs jobs inside the API process (single-process development/tests).

## Backend modules (`backend/app/`)

| Module | Responsibility |
|---|---|
| `core/` | Settings (env > YAML > defaults), structured logging with secret redaction, error codes, the dependency container. |
| `api/` | FastAPI routers (`v1/*`), dependencies (auth → principal → org isolation → RBAC), error mapping (no raw 500s). |
| `schemas/` | Request/response DTOs. |
| `database/` | SQLAlchemy models (32 tables), tenant-scoped repositories, Alembic migrations. |
| `models/` | Model layer: provider abstraction (`openai_compatible`, `vllm`, `ollama`), registry (YAML/env), health service, router, token budgets. |
| `agents/` | Agent interface (`BaseAgent`, `AgentSpec`, `AgentContext`, `AgentResult`), runtimes (`single_shot`, `stream_text`, `tool_loop`) and builtin agents. |
| `orchestrator/` | Intent, modes, plans (templates + validated LLM plans), executor, verification loop, evidence ledger, run events. |
| `tools/` | Tool interface, registry (26 tools), executor (validation → permission → approval → timeout → audit), change sets. |
| `rag/` | Parsers (code/Markdown/TXT/PDF/DOCX), chunking, embeddings, Qdrant store, hybrid retrieval, reranking, context assembly with citations. |
| `projects/` | Import (zip/git/local), scanner, symbol extraction, import graph, analyzers, indexing pipeline. |
| `memory/` | Short-term window, conversation summarisation, long-term user/project/task/agent memory (secrets rejected). |
| `security/` | Auth service, Argon2 passwords, JWT/refresh rotation, RBAC, secret detection/masking, encryption, path jail, command policy, audit. |
| `sandbox/` | Client for the sandbox runner + workspace archive builder. |
| `evaluation/` | Benchmark runner (coding/debugging/SQL/tool-calling/reasoning/RAG) with objective scoring. |
| `billing/` | Plans, entitlements, quotas, usage metering, pluggable payment provider. |
| `services/` | Use-case services: projects, documents, runs/approvals, job queue. |

## Model layer

* **Registry** — `config/models.yaml` (see `config/models.example.yaml`) with `${ENV}` / `${ENV:-default}`
  interpolation, or the single-model env vars `OWNAI_MODEL_URL`/`OWNAI_MODEL_NAME`. Models whose variables are
  unset are skipped with a visible warning. Admin overrides (enable/priority) are stored in the `models` table.
* **Providers** validate every boundary: transport errors, HTTP status, JSON shape, *non-empty content*. Error
  codes: `MODEL_CONNECTION_ERROR`, `MODEL_TIMEOUT`, `MODEL_HTTP_ERROR`, `MODEL_AUTH_ERROR`,
  `MODEL_INVALID_RESPONSE`, `MODEL_EMPTY_RESPONSE`, `MODEL_WRONG_NAME`, `MODEL_STREAM_ERROR`,
  `MODEL_CONTEXT_LENGTH_EXCEEDED`. `<think>` blocks and `reasoning_content` are never exposed.
* **Health** — `online` only after: model list endpoint answered → configured name is served → a real
  completion/embedding returned non-empty output (+ streaming for deep tests; vLLM `max_model_len` is checked
  against the configured context). Failed models are re-probed after 15 s; passive failures mark models offline.
* **Router** — hard filters (enabled, healthy, capabilities, context window) then an explainable score (role match,
  priority, complexity preference, benchmark quality, observed success/latency, configured preferences).
  Retryable failures fall back to the next candidate and the user is told (`notices`). Critic independence is a
  soft preference that never outweighs capability. Every call is recorded in `model_usage`.

## Agents

Agents are declared in `config/agents/*.yaml` (id, description, capabilities, runtime, input/output schema,
allowed tools, model role, prompts, verification rules). Adding/removing an agent is a config change.

| Runtime | Agents |
|---|---|
| `builtin` | planner, project_context, critic, responder, coding/refactoring/testing (`code_change`), debugging, security, sql, documentation |
| `stream_text` | rag, assistant (general LLM agent) |
| `single_shot` | code_review, architecture |
| `tool_loop` | git, docker, devops, research, data_analysis |

The tool loop uses native OpenAI tool calls when the routed model supports them, otherwise a constrained JSON
action protocol. Structured outputs are validated with Pydantic and repaired once; invalid output fails with
`AGENT_INVALID_OUTPUT`, empty output with `AGENT_EMPTY_RESPONSE`.

## Orchestration

```
request → intent (rules; small model only if unsure) → mode policy → plan
        → steps (agents + tools; events streamed)
        → code changes: syntax → tests in sandbox with the change overlaid → fix loop → lint
        → critic (deterministic evidence checks + independent model review)
        → approval checkpoint (run paused, state persisted)  ── resume after decision ──►
        → apply change set → tests after apply → responder report from the evidence ledger
```

Modes: `quick`, `developer`, `deep` (LLM planner, extra verification), `project`, `debug`, `review`,
`architecture`, `auto`. The responder builds the final report deterministically from recorded evidence; claims
such as "tests pass" without a passing test run are removed and flagged.

## Project intelligence & RAG

Scanner (ignore rules, `.gitignore`, binary/size limits, sensitive-file detection) → symbol extraction (Python via
`ast`; brace-aware extractors for JS/TS/JSX/TSX, Java, Go, Rust, C/C++; SQL objects; Markdown sections; YAML/JSON
keys) → import graph → analyzers (dependencies, frameworks, API endpoints, env-var names, DB usage, test setup) →
secret masking → symbol-aware chunking → PostgreSQL full-text + embeddings in Qdrant.

Retrieval = dense (Qdrant, tenant-filtered) + lexical (PostgreSQL `tsvector` with identifier splitting and symbol/path
boosts) fused with RRF, optional reranker, then a token-budgeted context with `[S#]` citations
(`file:start-end` + symbol). Without an embedding model it degrades to keyword search and says so.

## Security model (summary)

See `docs/SECURITY.md`. Tenant isolation on every query; RBAC; tools only through the executor; path jail;
secrets masked before storage/prompting/logging; credential files never indexed; code runs only in the
sandbox (no network, read-only root, dropped capabilities, resource limits); destructive operations require
approval; append-only audit log.

## Data model

`users, organizations, memberships, refresh_tokens, projects, project_files, symbols, file_dependencies,
index_jobs, db_connections, documents, document_chunks, conversations, messages, tasks, task_runs, run_steps,
run_events, tool_runs, approvals, changesets, changeset_files, models, model_usage, agents, memories,
evaluations, evaluation_results, plans, subscriptions, usage_records, audit_logs` (migration
`backend/migrations/versions/0001_initial_schema.py`).

## Known limitations

* **Answer quality depends on the models you serve.** The platform verifies and reports honestly, but small models
  (≤4B parameters) often misdiagnose bugs or fail structured-output formats; use 7B+ coding models (GPU) for the
  code-change workflows. On CPU-only hardware multi-agent runs take minutes (set `OWNAI_AGENT_TIMEOUT_SCALE`).
* Symbol extraction for non-Python languages is regex/brace based (robust for common code, not a full parser);
  the extractor is isolated so a tree-sitter backend can be added.
* The sandbox runs tests with the packages baked into the sandbox image (no network at run time by design);
  projects with extra dependencies need a custom sandbox image. Node/Go/Rust profiles need their images pulled.
* Linting is implemented for Python (ruff); other languages report "linter unavailable".
* Vision role: models can be registered and routed, but no agent currently sends images (no image upload in the UI).
* Payment providers are an interface only (no Stripe adapter shipped); plans are assigned manually or by license.
* SSO/OIDC, PostgreSQL row-level security, Helm charts and multi-region deployment are not implemented yet.
* Dependency vulnerability scanning is offline heuristics (unpinned versions) — no CVE database is bundled.
