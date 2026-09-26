# OwnAI — private multi-agent AI developer platform

OwnAI is a self-hosted AI workspace for programmers. It imports your projects, understands them (structure,
symbols, dependencies, API endpoints, tests), answers questions with cited sources, finds and fixes bugs, writes
and reviews code, runs tests in an isolated sandbox, and never changes your code or data without your approval.
It runs entirely on your own hardware with local models (vLLM, Ollama or any OpenAI-compatible server) — no
external AI API is required.

**Backend:** Python 3.12 + FastAPI (API, orchestration, agents, RAG, tools, security, workers)
**Frontend:** React + TypeScript + Vite
**Infrastructure:** PostgreSQL · Redis · Qdrant · Docker sandbox · vLLM/Ollama

## What it does

* **Multi-model routing** — configurable model registry (fast / coding / reasoning / vision / embedding / reranker),
  real health checks, explainable health-aware routing with fallback and user-visible notices.
* **Multi-agent orchestration** — 20 agents declared in YAML (planner, project context, coding, debugging, critic,
  code review, refactoring, testing, architecture, documentation, SQL, Git, Docker, DevOps, security, RAG,
  research, data analysis, …); only the agents a request needs are run.
* **Project intelligence & RAG** — zip/git/local import, symbol extraction for 10+ languages, dependency and
  endpoint analysis, hybrid semantic + keyword retrieval with `file:line` citations, PDF/DOCX/Markdown documents.
* **Verified code changes** — edits are staged as diffs; tests run in the sandbox *with* the proposed change,
  failures feed a fix loop, an independent critic reviews, you approve, the change is applied and tests run again.
  The final report is built from recorded evidence — it cannot claim tests passed unless they did.
* **Security** — org isolation, RBAC, path jail, secret detection/masking, credential files never sent to models,
  sandboxed execution (no network, read-only, resource limits), approval gates for destructive actions, audit log.
* **Operations** — SSE progress streaming, pipeline diagnostics that pinpoint *which layer* returned an empty AI
  answer, model/agent monitors, evaluation benchmarks that set routing priorities from measured results,
  plans/quotas architecture for commercial use.

## Quick start (Docker)

```bash
./scripts/setup.sh            # Windows: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# configure models: edit config/models.yaml (vLLM) or run ./scripts/setup.sh ollama
docker compose -f deploy/docker-compose.yml --env-file .env --profile build build
docker compose -f deploy/docker-compose.yml --env-file .env up -d
# GPU (vLLM):  add -f deploy/docker-compose.gpu.yml   ·   Ollama: add -f deploy/docker-compose.ollama.yml
```

Open http://localhost:8080, register (the first account is the administrator), check **Models** (a model is ONLINE
only after a real completion succeeded), then run **Admin → AI pipeline diagnostics**.

## Documentation

| | |
|---|---|
| [Installation](docs/INSTALL.md) — Docker, Linux, Windows/PowerShell, GPU/vLLM, Ollama, local dev | [Configuration](docs/CONFIGURATION.md) — environment, models, agents, plans |
| [Architecture](docs/ARCHITECTURE.md) | [Agents & tools](docs/AGENTS_AND_TOOLS.md) |
| [Security](docs/SECURITY.md) | [Sandbox](docs/SANDBOX.md) |
| [API & SSE events](docs/API.md) | [Testing](docs/TESTING.md) |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | |

## Repository layout

```
backend/          FastAPI app (app/), Alembic migrations, tests (unit, integration, fakes)
frontend/         React + TypeScript UI, vitest + Playwright tests
sandbox_runner/   isolated code-execution service + sandbox images
config/           models.example.yaml, models.ollama.example.yaml, agents/*.yaml, plans.example.yaml
evaluation/       benchmark datasets
deploy/           docker-compose.yml, docker-compose.gpu.yml (vLLM), docker-compose.ollama.yml
scripts/          setup.sh / setup.ps1
docs/             documentation
```

## Status and limitations

See the "Known limitations" section of [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#known-limitations).
