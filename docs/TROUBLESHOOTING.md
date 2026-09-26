# Troubleshooting

Start with **Admin → AI pipeline diagnostics** (or `POST /api/v1/admin/diagnostics/pipeline`). It checks every
boundary with real calls and reports the **first failing layer**:

`services → model:<id> (endpoint → /v1/models → model name → real completion content → streaming) →
router_provider_parser → agent → orchestrator (optional) → API → browser`

## "The server responds but the AI answer is empty"

OwnAI never treats HTTP 200 as success by itself. The failing layer tells you why:

| Code | Meaning | Fix |
|---|---|---|
| `MODEL_CONNECTION_ERROR` | Endpoint unreachable | Is vLLM/Ollama running? From Docker use `host.docker.internal` or the service name, not `localhost`. Ollama must listen on `0.0.0.0` to be reachable from containers. |
| `MODEL_HTTP_ERROR` (404) | Wrong URL path | OpenAI-compatible endpoints usually end in `/v1` (`http://host:8000/v1`); Ollama native uses `provider: ollama` and `http://host:11434`. |
| `MODEL_WRONG_NAME` | The server does not serve the configured name | Compare `model:` in `models.yaml` with `GET /v1/models` (vLLM `--served-model-name`, `ollama list`). The Models page shows the served names. |
| `MODEL_AUTH_ERROR` | 401/403 | Set `api_key` for that model. |
| `MODEL_EMPTY_RESPONSE` | HTTP 200 but no content | Often a reasoning model spending all tokens "thinking": raise `max_output_tokens`/`health_probe_max_tokens`, or start vLLM with the matching `--reasoning-parser`. Also check the chat template. |
| `MODEL_INVALID_RESPONSE` | Non-JSON / wrong shape | A proxy or non-OpenAI server is answering. Check the URL. |
| `MODEL_STREAM_ERROR` | Stream malformed or cut off | Proxy buffering/timeouts; with nginx use the provided config (`proxy_buffering off`). |
| `MODEL_CONTEXT_LENGTH_EXCEEDED` | Prompt larger than the server's context | The hint shows the server's real window. vLLM: `--max-model-len`; Ollama: use `provider: ollama` (the `/v1` endpoint uses Ollama's default context). Lower `context_length` in `models.yaml` to match. |
| `MODEL_UNAVAILABLE` | No healthy model for the role | Check the Models page; at least one chat model and (for semantic search) one embedding model must be online. |
| `AGENT_INVALID_OUTPUT` | The model did not follow the required JSON format (even after one repair round) | Use a stronger/instruction-tuned model for that role, or enable `supports_json_schema` (vLLM guided decoding / Ollama `format`). Invalid outputs are logged as `agent.invalid_structured_output` with a preview. |
| `AGENT_EMPTY_RESPONSE` / `ORCHESTRATOR_EMPTY_RESPONSE` | An agent/run produced nothing | Always a real bug or a model problem — see the worker log for the run ID. |
| `FRONTEND_RESPONSE_ERROR` | The browser received an empty/invalid response | Check the reverse proxy (nginx), CORS origins and the API logs for the `request_id`. |

## Models show "offline" but work with curl

* The health check needs `GET /v1/models` (or `/api/tags` for Ollama) **and** a real completion with content.
  Use *Test connection* on the Models page to see each step.
* Health is shared through Redis between API and worker; failed models are re-checked every 15 s.

## Semantic search disabled

Without a healthy embedding model OwnAI falls back to keyword search and says so. After fixing the embedding
model, re-index the project. Changing `embedding_dimensions`/model requires a re-index (a new Qdrant collection is
created per model).

## Code execution fails

* `SANDBOX_UNAVAILABLE`: start `sandbox-runner` and check `OWNAI_SANDBOX_URL` / token (`GET /health` shows it).
* "sandbox container could not start": build the image `docker compose ... --profile build build`
  (`ownai/sandbox-python:3.12`).
* Tests need packages that are not in the sandbox image: extend `sandbox_runner/images/python/Dockerfile`
  (dependency installation at run time is intentionally disabled — no network in the sandbox).

## Slow on CPU

Every agent step is a model call. On CPU use Quick mode for simple questions, small fast models for the `fast`
role, and keep `max_output_tokens` modest. The background health loop only runs a real generation probe every
10 minutes per model.

## Windows

* Run from a folder outside OneDrive (e.g. `C:\dev\ownai`).
* If `scripts\setup.ps1` is blocked: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`.
* `.env` is read automatically by the backend, including `${VARS}` used in `config/models.yaml`.

## Logs

* Compose: `docker compose -f deploy/docker-compose.yml logs -f api worker sandbox-runner`
* Every response carries `X-Request-ID`; errors include `request_id` — search the logs for it.
