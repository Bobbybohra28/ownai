# Agents and tools

## How requests are handled

The user does not choose agents. The orchestrator detects the intent (keyword rules first, a small model only
when unsure), picks a mode policy, builds a plan (workflow template, or a validated model-generated plan in Deep
mode) and runs only the agents that plan needs.

| Intent (examples) | Plan |
|---|---|
| explain / search code ("How does auth work?") | project_context → rag (streamed, cited) → critic |
| fix_bug ("Find the auth bug and fix it") | project_context → debugging (runs tests in sandbox) → coding (staged diff) → syntax/tests/lint in sandbox (fix loop) → critic → **approval** → apply → tests again |
| debug / analyze logs | project_context → debugging → critic |
| generate / modify code | project_context → coding → verification → critic → approval |
| refactor / write tests | project_context → refactoring / testing → verification → critic → approval |
| review / architecture / docs | project_context → code_review / architecture / documentation → critic |
| sql | project_context → sql (schema from the live DB, validated, read-only execution; writes need approval) |
| security | project_context → security (deterministic scanners + model triage) |
| git / docker / devops / data | tool-loop agents with read-only or sandboxed tools |
| general questions (no project) | assistant (streamed) |

## Agent specification

```yaml
id: debugging                 # unique id
name: Debugging Agent
description: …                # the planner chooses agents by this description
category: development         # core | development | data | devops | security | knowledge
capabilities: [debugging, root_cause_analysis]
runtime: builtin              # builtin | single_shot | stream_text | tool_loop
implementation: debugging     # builtin class (planner, project_context, critic, responder, code_change,
                              #                debugging, security, sql, documentation)
input_schema: AgentTask
output_schema: DebugReport    # AnswerOutput | AnalysisReport | DebugReport | CodeChangeProposal |
                              # CriticVerdict | PlanOutput | SQLOutput | DocumentationOutput
allowed_tools: [read_file, search_code, run_tests, read_logs, git_diff]
model_role: coding            # preferred role; complex_model_role for complex tasks
complex_model_role: reasoning
max_output_tokens: 900
temperature: 0.1
timeout_s: 600                # scaled by OWNAI_AGENT_TIMEOUT_SCALE
context_budget_tokens: 3500
requires_project: true
system_prompt: |
  …
verification: {critic: true, run_tests: false, run_lint: false, syntax_check: false, require_citations: false}
```

To add an agent, drop a YAML file into `config/agents/` using an existing runtime (e.g. a `single_shot` reviewer
for a new language, or a `tool_loop` agent with a new tool set) and restart. New control flow = a new builtin class
registered in `backend/app/agents/registry.py`.

### Built-in agents (20)

planner, project_context, critic, responder, assistant (LLM agent), rag, coding, refactoring, testing, debugging,
code_review, architecture, documentation, sql, git, docker, devops, security, research (documentation search),
data_analysis. The model router is deterministic code (`models/router.py`), shown on the Models page.

## Tools (26)

| Tool | Permission | Notes |
|---|---|---|
| `read_file` | read | line ranges, secrets masked, `.env` → variable names only |
| `project_structure`, `scan_project` | read | tree; languages/frameworks/deps/endpoints/env names/tests + changes since last index |
| `search_code` | read | text/regex across files (ignore rules applied) |
| `semantic_search` | read | hybrid RAG with citations |
| `write_file`, `edit_file`, `delete_file`, `create_changeset` | write_staged | edits go to a change set, never directly to files |
| `apply_changeset` | destructive | approval required; conflict-safe |
| `run_python`, `run_tests`, `run_lint` | execute | sandbox only; tests can overlay the staged change set |
| `run_sql` | read → approval for writes | parser-based classification |
| `inspect_database`, `validate_sql` | read | |
| `git_status`, `git_diff`, `git_log`, `git_branch` | read | safe git config |
| `git_checkout`, `git_commit` | destructive | approval; never pushes |
| `docker_status`, `docker_logs`, `inspect_process` | read | project Docker config + containers OwnAI started |
| `read_logs` | read | `*.log` / `logs/` only, tail + grep, secrets masked |

Every tool call is persisted in `tool_runs` (redacted arguments/outputs, duration, status) and non-read actions and
denials in `audit_logs`.

## Adding a tool

Subclass `app.tools.base.Tool` (Pydantic `args_model`, `permission`, `timeout_s`, optional `approval_needed()`),
register it in `app/tools/registry.py` and allow it for agents via `allowed_tools`.
