"""Code-change agent (used by the Coding, Refactoring and Testing agent specs).

The model proposes structured edits (exact find/replace or full-file writes). Edits are
applied only to a *change set* through the audited tool layer — never directly to the
project. Edits that cannot be applied (e.g. ``find`` text not found) are fed back to the
model for one repair round.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent, task_prompt
from app.agents.runtimes.single_shot import output_instructions, structured_call
from app.agents.schemas import CodeChangeProposal, FileEdit
from app.core.exceptions import AgentError, ErrorCode
from app.models.providers.base import ChatMessage
from app.projects.scanner import is_test_path
from app.security.secrets import is_sensitive_file, mask_secrets

MAX_FILES = 4
MAX_FILE_LINES = 700
_PATH_TOKEN = re.compile(r"[\w./\-]+\.[A-Za-z0-9]{1,6}")

EDIT_GUIDE = """How to propose edits:
- Use action "replace" with `find` = an exact, unique snippet copied verbatim from the file (including indentation) and `replace` = the new text. Keep snippets small (a few lines).
- Use action "create" (new file) or "rewrite" (whole file) with the complete `content`. For small files (under ~80 lines) "rewrite" is acceptable when several places change.
- `path` must be exactly one of the project paths shown below (e.g. "src/app/auth.py"), written once.
- Use action "delete" only if the task requires removing a file.
- Make the minimal change that fulfils the task. Do not reformat unrelated code.
- Never hard-code secrets; read them from configuration/environment.
- Never weaken, skip or delete existing tests to make them pass."""


class CodeChangeAgent(BaseAgent):
    def _candidate_files(self, task: AgentTask, ctx: AgentContext) -> list[str]:
        assert ctx.project is not None
        jail = ctx.project.jail
        ordered: list[str] = []

        def add(path: str | None) -> None:
            if not path:
                return
            try:
                rel = jail.normalize(path.split(":")[0])
            except Exception:  # invalid path from model output: ignore
                return
            if rel and rel not in ordered and not is_sensitive_file(rel) and (ctx.project.root / rel).is_file():
                ordered.append(rel)

        for output in task.inputs.values():
            if isinstance(output, dict):
                for path in output.get("affected_files") or []:
                    add(path)
                for ev in output.get("evidence") or []:
                    if isinstance(ev, dict):
                        add(ev.get("file"))
        for token in _PATH_TOKEN.findall(task.request):
            add(token)
        for citation in task.citations:
            add(citation.get("file_path"))
        tests_task = self.id == "testing" or task.intent == "write_tests"
        if not tests_task:
            # implementation files first; tests are context only
            ordered.sort(key=lambda p: is_test_path(p))
        return ordered[:MAX_FILES]

    def _file_blocks(self, ctx: AgentContext, files: list[str]) -> str:
        assert ctx.project is not None
        blocks = []
        for rel in files:
            text = (ctx.project.root / rel).read_text(encoding="utf-8", errors="replace")
            lines = text.split("\n")
            if len(lines) > MAX_FILE_LINES:
                text = "\n".join(lines[:MAX_FILE_LINES]) + f"\n… ({len(lines) - MAX_FILE_LINES} more lines not shown)"
            blocks.append(f"File: {rel}\n```\n{mask_secrets(text)}\n```")
        return "\n\n".join(blocks)

    async def _apply(self, edits: list[FileEdit], task: AgentTask, ctx: AgentContext, result: AgentResult,
                     allow_tests: bool) -> tuple[list[str], list[str]]:
        applied: list[str] = []
        failures: list[str] = []
        for edit in edits:
            if not allow_tests and is_test_path(edit.path) and edit.action in ("replace", "rewrite", "delete"):
                failures.append(f"{edit.path}: editing existing tests is not allowed for this task (fix the code instead).")
                continue
            if edit.action == "replace":
                if not edit.find or edit.replace is None:
                    failures.append(f"{edit.path}: 'replace' needs both find and replace.")
                    continue
                outcome = await self.use_tool(ctx, task, "edit_file",
                                              {"path": edit.path, "find": edit.find, "replace": edit.replace}, result)
            elif edit.action in ("create", "rewrite"):
                if edit.content is None:
                    failures.append(f"{edit.path}: '{edit.action}' needs content.")
                    continue
                outcome = await self.use_tool(ctx, task, "write_file", {"path": edit.path, "content": edit.content}, result)
            else:
                outcome = await self.use_tool(ctx, task, "delete_file", {"path": edit.path}, result)
            if outcome.ok:
                applied.append(edit.path)
                result.changeset_id = outcome.data.get("changeset_id") or result.changeset_id
            else:
                failures.append(f"{edit.path}: {outcome.summary}")
        return applied, failures

    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        if ctx.project is None:
            raise AgentError("Code changes need a project. Import or select a project first.",
                             code=ErrorCode.INSUFFICIENT_CONTEXT)
        files = self._candidate_files(task, ctx)
        await ctx.status(f"{self.spec.name}: preparing changes…")
        allow_tests = self.id == "testing" or task.intent == "write_tests" or "test" in task.request.lower()
        extra = EDIT_GUIDE + "\n\n" + (self._file_blocks(ctx, files) if files else
                                       "No specific files were identified; create new files only if appropriate.")
        baseline = ctx.evidence.latest("tests", where={"phase": "baseline"})
        if baseline is not None and baseline.status == "failed" and not task.feedback:
            extra += ("\n\nFailing tests before your change (your fix must make them pass):\n```\n"
                      + str(baseline.data.get("output_tail", ""))[-2500:] + "\n```")
        messages = [
            ChatMessage(role="system", content=self.system_prompt(output_instructions(CodeChangeProposal))),
            ChatMessage(role="user", content=task_prompt(task, extra=extra)),
        ]
        proposal = await structured_call(self, ctx, task, messages, CodeChangeProposal, result)
        if not proposal.edits:
            raise AgentError(f"The {self.spec.name} did not propose any edits.", code=ErrorCode.AGENT_EMPTY_RESPONSE,
                             detail=proposal.summary)
        applied, failures = await self._apply(proposal.edits, task, ctx, result, allow_tests)
        if failures:
            await ctx.status(f"{self.spec.name}: repairing {len(failures)} edit(s)…")
            repair = [*messages, ChatMessage(role="assistant", content=proposal.model_dump_json()),
                      ChatMessage(role="user", content="These edits could not be applied:\n- " + "\n- ".join(failures)
                                  + "\nReturn corrected edits ONLY for the failed files. Copy `find` text exactly "
                                    "from the file contents shown above. " + output_instructions(CodeChangeProposal))]
            retry = await structured_call(self, ctx, task, repair, CodeChangeProposal, result)
            failed_paths = {f.split(":")[0] for f in failures}
            more_applied, failures = await self._apply([e for e in retry.edits if e.path in failed_paths] or retry.edits,
                                                       task, ctx, result, allow_tests)
            applied += more_applied
        if not applied:
            raise AgentError("None of the proposed edits could be applied.", code=ErrorCode.AGENT_FAILED,
                             detail="; ".join(failures))
        result.summary = proposal.summary
        result.output = {"summary": proposal.summary, "files": sorted(set(applied)), "notes": proposal.notes,
                         "unapplied": failures, "changeset_id": result.changeset_id}
        if failures:
            result.notices.append(f"{len(failures)} proposed edit(s) could not be applied: " + "; ".join(failures)[:500])
        await ctx.emit("changeset", changeset_id=result.changeset_id, files=sorted(set(applied)))

    @staticmethod
    def describe(output: dict[str, Any]) -> str:
        return output.get("summary", "")
