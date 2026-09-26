# Security model

## Principles

1. **The AI never gets unrestricted host access.** Models only *propose* tool calls; every call passes through
   `ToolExecutor` (`backend/app/tools/executor.py`), which validates arguments, checks the agent's tool allowlist,
   the user's role and the tool's permission level, routes risky calls to human approval, enforces timeouts and
   writes the audit trail.
2. **Content from projects and documents is untrusted data.** Prompts wrap it in `<context>` blocks and instruct
   models to ignore embedded instructions — but safety never depends on the model obeying: permissions,
   approvals and the sandbox are enforced in code.
3. **Secrets never reach models, memory or logs.**

## Authentication & authorization

* Argon2id password hashing; login timing does not reveal whether an account exists.
* Short-lived JWT access tokens (in memory in the browser) + rotating opaque refresh tokens (httpOnly,
  `SameSite=Strict` cookie scoped to `/api/v1/auth`). Reuse of a rotated refresh token revokes the whole token
  family. Cookie-authenticated endpoints additionally require `X-Requested-With: ownai` (CSRF defence).
* Organisation isolation: every tenant-owned query is scoped by `organization_id` (`database/repositories/base.py`);
  cross-tenant access returns 404. Conversations are private to their author; projects are shared within an org.
* RBAC (`security/rbac.py`): `viewer` (read), `member` (chat, execute in sandbox, stage changes, decide approvals),
  `admin` (members, models, agents, evaluations, audit), `owner` (+ billing). The first registered account is the
  installation administrator.

## Tool permission levels

| Level | Examples | Rule |
|---|---|---|
| `read` | read_file, search_code, semantic_search, git_status/diff/log, inspect_database | viewer+ |
| `write_staged` | write_file, edit_file, delete_file (into a change set) | member+; never touches project files |
| `execute` | run_python, run_tests, run_lint | member+; sandbox only |
| `destructive` | apply_changeset, git_commit, git_checkout, write SQL | **always requires approval** |

`run_sql` classifies every statement with a real SQL parser (sqlglot): `SELECT` runs in a read-only transaction
with a statement timeout and row cap; `INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/GRANT…` require approval (and are
blocked entirely on connections registered as read-only). `UPDATE`/`DELETE` without `WHERE` are flagged critical.

## Code changes

AI edits are staged in a **change set** with a unified diff and the hash of each file at proposal time. Applying
requires approval; if a file changed after the proposal the apply is refused (`CHANGESET_CONFLICT`) so your work is
never overwritten. Staging is refused when the new content would introduce a hard-coded secret. Existing tests
cannot be edited by the Coding agent (only by the Testing agent or when the request is about tests).

## File access

`PathJail` (`security/paths.py`) resolves every path inside the project root, rejects `..`, absolute paths outside
the root and symlink escapes, blocks `.git/` internals and refuses credential files (`.env*`, keys, `*.pem`,
`credentials.json`, …). For `.env` files only variable **names** are shown.

## Secrets

`security/secrets.py` detects private keys, cloud/API tokens (AWS, GitHub, GitLab, Slack, Stripe, Google,
OpenAI/Anthropic-style keys), JWTs, credentials in URLs and `password = "…"`-style assignments.
Detected values are masked (line structure preserved) before indexing, embedding, prompting, logging, audit
records, tool outputs and memory. Memory writes containing secrets are rejected. Customer database passwords are
encrypted at rest with Fernet (`OWNAI_ENCRYPTION_KEY`, rotation via comma-separated keys).

## Sandbox

`sandbox_runner` is a separate service and the only component with container-runtime access. Each execution:

* receives the workspace as a tar stream over stdin (no host bind mounts), credential files excluded;
* runs with `--network none`, `--read-only` root filesystem, workspace on tmpfs, non-root UID 1000,
  `--cap-drop ALL`, `--security-opt no-new-privileges`, PID/memory (no swap)/CPU limits, wall-clock timeout,
  truncated output; the container is removed afterwards;
* only allowlisted executables per profile (`python`, `pytest`, `ruff`, …); dependency installation
  (`pip install`, `npm install`, …) is refused;
* optional gVisor runtime: `SANDBOX_RUNTIME=runsc` (recommended for multi-tenant deployments).

Verified behaviour (see `docs/TESTING.md`): network unreachable, timeouts killed, read-only root, OOM detected,
fork bombs contained, disallowed commands rejected. **Note:** the runner mounts the Docker socket; run it on a host
dedicated to OwnAI and never expose its port (8090) outside the internal network. Always set `SANDBOX_TOKEN`.

## Git

Git runs with hooks, fsmonitor, external diff/textconv, credential helpers and protocol transports disabled.
Imported repositories are sanitised (hooks removed; config sections that can execute programs stripped). Clones
are shallow, without submodules, `file://`/`ext::` transports are rejected. OwnAI never pushes.

## Imports

Zip uploads are checked for path traversal (zip-slip), absolute paths, symlinks, entry count and expanded size.
Local-path imports are restricted to `OWNAI_LOCAL_IMPORT_ROOTS`.

## Audit & logging

Append-only `audit_logs` records authentication, approvals, tool executions (non-read and all denials), admin
changes, imports and deletions with request IDs. Structured JSON logs carry `request_id`, `user_id`, `org_id`,
`project_id`, `conversation_id`, `task_id`, `run_id`, agent/model/tool and durations; a log processor redacts
sensitive keys and secret patterns.

## Hardening checklist for production

* Serve behind TLS; set `OWNAI_COOKIE_SECURE=true`, `OWNAI_APP_ENV=production`, restrict `OWNAI_CORS_ORIGINS`.
* Set strong `OWNAI_SECRET_KEY`, `OWNAI_ENCRYPTION_KEY`, `SANDBOX_TOKEN`; disable `OWNAI_ALLOW_REGISTRATION` after
  creating accounts.
* Run the sandbox with gVisor; keep Postgres/Redis/Qdrant on an internal network (the compose file does not publish
  their ports).
* Register customer databases with read-only DB roles.
