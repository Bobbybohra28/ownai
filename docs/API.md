# API reference

Interactive OpenAPI docs: `http://<host>:8000/api/v1/docs` (JSON schema at `/api/v1/openapi.json`).
All endpoints are under `/api/v1` and require `Authorization: Bearer <access token>` unless noted.

## Errors

Every error has the same shape — never a bare `500 Internal Server Error`:

```json
{"error": {"code": "MODEL_EMPTY_RESPONSE", "message": "The model 'coding' returned an empty response…",
           "hint": "…", "request_id": "3f2c…"}}
```

Common codes: `AUTHENTICATION_REQUIRED`, `INVALID_CREDENTIALS`, `PERMISSION_DENIED`, `NOT_FOUND`, `VALIDATION_ERROR`,
`QUOTA_EXCEEDED`, `MODEL_CONNECTION_ERROR`, `MODEL_TIMEOUT`, `MODEL_HTTP_ERROR`, `MODEL_AUTH_ERROR`,
`MODEL_INVALID_RESPONSE`, `MODEL_EMPTY_RESPONSE`, `MODEL_WRONG_NAME`, `MODEL_STREAM_ERROR`,
`MODEL_CONTEXT_LENGTH_EXCEEDED`, `MODEL_UNAVAILABLE`, `AGENT_EMPTY_RESPONSE`, `AGENT_INVALID_OUTPUT`,
`ORCHESTRATOR_EMPTY_RESPONSE`, `SANDBOX_UNAVAILABLE`, `PATH_NOT_ALLOWED`, `APPROVAL_REQUIRED`, `CHANGESET_CONFLICT`.
The frontend adds `FRONTEND_RESPONSE_ERROR` for empty/malformed responses and `BACKEND_UNREACHABLE`.

## Endpoints

| Area | Endpoints |
|---|---|
| System | `GET /health` (no auth) |
| Auth | `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh` (cookie + `X-Requested-With: ownai`), `POST /auth/logout`, `GET /auth/me` |
| Users | `GET/PATCH /users/me`, `POST /users/me/password` |
| Organizations | `GET /orgs`, `GET /orgs/current`, `POST /orgs/{id}/switch`, `GET/POST /orgs/current/members`, `PATCH/DELETE /orgs/current/members/{user_id}` |
| Projects | `GET/POST /projects`, `POST /projects/import/upload` (multipart zip), `POST /projects/import/git`, `POST /projects/import/local`, `GET/DELETE /projects/{id}`, `POST /projects/{id}/reindex`, `GET /projects/{id}/index-status`, `GET /projects/{id}/overview`, `GET /projects/{id}/symbols?q=`, `POST /projects/{id}/scan` |
| Change sets | `GET /projects/{id}/changesets`, `GET /projects/{id}/changesets/{cs}` (with diffs), `POST /projects/{id}/changesets/{cs}/discard` (apply = approval) |
| Files & documents | `GET /projects/{id}/files`, `GET /projects/{id}/files/content?path=`, `GET/POST /documents`, `GET/DELETE /documents/{id}` |
| Conversations | `GET/POST /conversations`, `GET/PATCH/DELETE /conversations/{id}`, `GET /conversations/{id}/messages`, `POST /conversations/{id}/messages` → `{run_id}` |
| Runs | `GET /runs`, `GET /runs/{id}` (status, steps, result), `GET /runs/{id}/tool-runs`, `POST /runs/{id}/cancel`, `GET /runs/{id}/events` (SSE) |
| Approvals | `GET /approvals?status=`, `GET /approvals/{id}`, `POST /approvals/{id}/approve`, `POST /approvals/{id}/reject` |
| Models | `GET /models`, `GET /models/health?refresh=`, `GET /models/{id}`, `POST /models/{id}/test`, `PATCH /models/{id}` (admin), `GET /models/routing/preview`, `GET /models/usage` |
| Agents | `GET /agents`, `GET /agents/{id}`, `PATCH /agents/{id}` (admin), `GET /agents/activity` |
| Tools | `GET /tools`, `POST /tools/{name}/invoke` (same policy as agents) |
| RAG | `POST /rag/search` `{query, project_id?, top_k, language?, document_type?, path_prefix?, answer?}` |
| Memory | `GET/POST /memory`, `PATCH/DELETE /memory/{id}` |
| Git | `GET /projects/{id}/git/status|diff|log|branches`, `POST /projects/{id}/git/branches`, `POST /projects/{id}/git/checkout` (approval), `POST /projects/{id}/git/commit` (approval) |
| SQL | `GET/POST /projects/{id}/db-connections`, `DELETE /db-connections/{id}`, `GET /db-connections/{id}/schema`, `POST /db-connections/{id}/query`, `POST /sql/validate` |
| Evaluation | `GET /evaluation/datasets`, `POST/GET /evaluation/runs`, `GET /evaluation/runs/{id}`, `GET /evaluation/leaderboard`, `POST /evaluation/runs/{id}/apply-priority` (admin) |
| Admin | `GET /admin/health`, `GET /admin/audit-logs`, `GET /admin/usage`, `GET /admin/users`, `POST /admin/diagnostics/pipeline?full=` |
| Billing | `GET /billing/plans`, `GET /billing/subscription`, `POST /billing/plan`, `POST /billing/checkout`, `POST /billing/webhook/{provider}` |

## Chat flow

```http
POST /api/v1/conversations            {"project_id": "…", "mode": "auto"}
POST /api/v1/conversations/{id}/messages   {"content": "Find the authentication bug and fix it."}
→ 202 {"message_id": "…", "task_id": "…", "run_id": "…"}
GET  /api/v1/runs/{run_id}/events     (text/event-stream; resume with Last-Event-ID)
```

### SSE events

`run_started`, `status` (safe progress text), `intent`, `plan`, `step_started`, `step_finished`, `model_selected`,
`notice` (fallbacks, degraded retrieval), `tool_started`, `tool_finished`, `sources`, `changeset`, `verification`
(syntax/tests/lint/critic), `token` (streamed answer text), `answer` (final report), `approval_required`,
`approval_resolved`, `error`, `run_paused` (waiting for approval — stream ends), `run_finished` (stream ends).
Model reasoning is never streamed.

After `run_paused`, approve with `POST /approvals/{id}/approve`; the run resumes in the worker — reopen the event
stream to follow it.

## Modes

`auto` (default), `quick`, `developer`, `deep` (LLM planner + extra verification), `project`, `debug`, `review`,
`architecture`.
