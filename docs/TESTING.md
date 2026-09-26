# Testing

## Backend unit tests (no services required)

```bash
cd backend && pytest tests/unit
```

Covers: secret detection/masking, path jail, command policy, JWT/passwords/crypto/RBAC, every provider failure
mode (empty content, reasoning-only output, invalid JSON, 401/403/404/500, wrong model name, context overflow,
connection refused, timeout, malformed/truncated streams, embedding validation, Ollama native API), health checks,
router selection/fallback/independence, registry interpolation, scanner/.gitignore, symbol extraction, dependency
parsing, import graph, chunk line ranges, zip-slip, git sanitisation, intent rules, plan templates/validation,
JSON extraction, the claims guard, edit helpers, SQL classification.

## Integration tests (PostgreSQL + Qdrant, optional sandbox)

They start the real FastAPI app (inline execution mode) against a real database and vector store and a **fake
OpenAI-compatible model server** (`tests/fakes/fake_model_server.py`, a test double with scriptable failure modes).

```bash
export OWNAI_TEST_DATABASE_URL=postgresql+asyncpg://ownai:ownai@localhost:5432/ownai_test   # will be reset!
export OWNAI_TEST_QDRANT_URL=http://localhost:6333
export OWNAI_TEST_SANDBOX_URL=http://localhost:8090 OWNAI_TEST_SANDBOX_TOKEN=<token>        # optional
pytest tests/integration
```

Covers: auth lifecycle (refresh rotation, errors), real model health (empty output ⇒ offline), project import →
indexing → overview → hybrid RAG with citations, org isolation, `.env` protection, path traversal, the full chat
flow over SSE, empty model output surfaced as an explicit error, pipeline diagnostics pinpointing the failing
boundary, role permissions & audit, SQL read vs. write-with-approval, and (with the sandbox) the complete
**find bug → diagnose → stage fix → run tests with the change in the sandbox → approval → apply → re-test** flow.

## Frontend

```bash
cd frontend
npm run typecheck && npm test      # vitest (SSE parser, run-state reducer)
npm run build
```

## Real models

The fake server proves the pipeline mechanics. To test with real models, configure `config/models.yaml`, start the
stack and use **Models → Test connection**, **Admin → AI pipeline diagnostics (with full orchestrator run)** and the
**Evaluation** page (coding/debugging cases are verified by executing the generated code in the sandbox).

## Sandbox isolation tests

`backend/tests/integration/test_sandbox.py` (needs `OWNAI_TEST_SANDBOX_URL`) verifies against the real runner and
Docker: code execution, running the fixture project's tests, credential files not shipped, no network, timeout kill,
read-only root + non-root user, memory limit, and rejection of disallowed commands.

## UI end-to-end (Playwright)

```bash
cd frontend
OWNAI_UI_URL=http://localhost:5173 npx playwright test      # needs a running stack with at least one online model
```

Registers a user, opens every page, sends a Quick-mode question and waits for the persisted, model-attributed
final answer (fails on any page error or a failed run).
