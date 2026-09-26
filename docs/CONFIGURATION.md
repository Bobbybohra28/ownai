# Configuration

Precedence: environment variables / `.env` > YAML file named by `OWNAI_CONFIG_FILE` > defaults.
Secrets are only read from the environment. Relative paths resolve against the repository root.

## Environment variables (`.env`, see `.env.example`)

| Variable | Default | Purpose |
|---|---|---|
| `OWNAI_SECRET_KEY` | — (required, ≥32 chars) | JWT signing key |
| `OWNAI_ENCRYPTION_KEY` | — (required, Fernet key; comma-separated for rotation) | Encrypts stored DB credentials |
| `OWNAI_APP_ENV` | `development` | `production` enforces a sandbox token |
| `OWNAI_EXECUTION_MODE` | `worker` | `worker` (Redis + worker process) or `inline` (single process) |
| `OWNAI_DATABASE_URL` | `postgresql+asyncpg://ownai:ownai@localhost:5432/ownai` | PostgreSQL |
| `OWNAI_REDIS_URL` | `redis://localhost:6379/0` | Job stream, run events, shared model health |
| `OWNAI_QDRANT_URL` / `OWNAI_QDRANT_API_KEY` | `http://localhost:6333` | Vector store |
| `OWNAI_PROJECTS_ROOT` | `data/projects` | Where imported projects and documents are stored |
| `OWNAI_LOCAL_IMPORT_ROOTS` | empty (disabled) | Comma-separated directories allowed for "import local folder" |
| `OWNAI_MODELS_CONFIG_PATH` | — | Model registry YAML (recommended) |
| `OWNAI_MODEL_URL` / `_NAME` / `_API_KEY` / `_PROVIDER` / `_CONTEXT_LENGTH` / `_SUPPORTS_TOOLS` | — | Single-model quick setup (used when no registry file) |
| `OWNAI_EMBEDDING_URL` / `_MODEL` / `_DIMENSIONS` / `_PROVIDER` | — | Embedding model for the quick setup |
| `OWNAI_MODEL_REQUEST_TIMEOUT_S` | `180` | Default per-request model timeout |
| `OWNAI_MODEL_HEALTH_INTERVAL_S` / `_TTL_S` | `60` / `300` | Background health loop interval / freshness |
| `OWNAI_AGENT_TIMEOUT_SCALE` | `1.0` | Multiplies every agent's `timeout_s` (e.g. `3` for CPU-only serving) |
| `OWNAI_MAX_RUN_SECONDS` | `1800` | Hard limit per task run |
| `OWNAI_MAX_FIX_ITERATIONS` | `2` | Upper bound for generate → test → fix loops |
| `OWNAI_SANDBOX_URL` / `OWNAI_SANDBOX_TOKEN` | `http://localhost:8090` / — | Sandbox runner |
| `SANDBOX_TOKEN` | — | Token configured on the runner (must equal `OWNAI_SANDBOX_TOKEN`) |
| `OWNAI_CORS_ORIGINS` | `http://localhost:5173` | Allowed browser origins |
| `OWNAI_COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `OWNAI_ALLOW_REGISTRATION` | `true` | Disable after creating accounts |
| `OWNAI_BILLING_ENABLED` / `OWNAI_DEFAULT_PLAN` / `OWNAI_PLANS_CONFIG_PATH` | `false` / `private` / built-in | Plans & quotas |
| `OWNAI_LOG_LEVEL` / `OWNAI_LOG_JSON` | `INFO` / `true` | Logging |

Sandbox runner variables: `SANDBOX_TOKEN`, `SANDBOX_RUNTIME` (`runsc` for gVisor), `SANDBOX_MEMORY_MB`,
`SANDBOX_MAX_TIMEOUT_S`, `SANDBOX_PIDS_LIMIT`, `SANDBOX_MAX_CONCURRENT`, `SANDBOX_PROFILES_JSON` (custom images).

## Model registry (`config/models.yaml`)

```yaml
models:
  coding:                               # model id used across the platform
    provider: vllm                      # openai_compatible | vllm | ollama
    endpoint: ${CODING_MODEL_URL:-http://localhost:8002/v1}
    api_key: ${CODING_MODEL_API_KEY:-}
    model: ${CODING_MODEL_NAME}         # name served by the endpoint
    roles: [coding]                     # fast | coding | reasoning | vision | embedding | reranker
    context_length: 32768
    max_output_tokens: 4096
    priority: 60                        # 0-100, admin-adjustable (Models page / evaluations)
    timeout_s: 180
    capabilities: {supports_tools: true, supports_json_schema: true, supports_vision: false}
    health_probe_max_tokens: 64         # raise for reasoning models
    family: qwen                        # optional, informational
  embedding:
    provider: vllm
    endpoint: ${EMBEDDING_MODEL_URL}
    model: ${EMBEDDING_MODEL_NAME}
    roles: [embedding]
    embedding_dimensions: 768           # changing this requires re-indexing
    embedding_query_prefix: "search_query: "        # model-specific task prefixes (e.g. nomic)
    embedding_document_prefix: "search_document: "
routing:
  role_preferences: {coding: [coding, reasoning]}
  allow_cross_role_fallback: true
  prefer_reasoning_for_complex: true
```

* A model whose required variables are unset is skipped with a warning (shown on the Models page).
* One model may serve several roles; with a single chat model everything still works.
* Add/remove/replace models by editing this file and restarting API + worker. Admins can disable models and
  change priorities at runtime (stored in the database).

## Agents (`config/agents/*.yaml`)

See `docs/AGENTS_AND_TOOLS.md`. Agents can be enabled/disabled at runtime on the Agents page.

## Plans (`config/plans.example.yaml`)

`free`, `pro`, `team`, `enterprise`, `private` with limits (`requests_per_month`, `projects`, `members`,
`indexed_files`) and entitlements (`model_tiers`, `deep_mode`, `project_memory`, …). Enforced only when
`OWNAI_BILLING_ENABLED=true`; otherwise the `private` plan (no limits) applies. Payment providers plug in through
`billing.service.PaymentProvider` — none is required.
