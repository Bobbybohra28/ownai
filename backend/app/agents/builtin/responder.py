"""Responder: assembles the final user-facing report from agent outputs and the evidence ledger.

Deterministic by design — the sections about tests, changes, verification and sources
are rendered from recorded facts, so the report cannot claim anything that did not
happen (e.g. "tests passed" without a passing test run).
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent
from app.orchestrator.evidence import EvidenceLedger

_TEST_CLAIM = re.compile(r"(?i)\b(all\s+)?tests?\s+(now\s+)?(pass(ed|es)?|succeed(ed|s)?|are\s+passing|green)\b")
_SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}


def guard_claims(text: str, ledger: EvidenceLedger) -> tuple[str, bool]:
    """Remove unverifiable test-success claims from model-written text."""
    if not text or not _TEST_CLAIM.search(text):
        return text, False
    if ledger.tests_passed() is True:
        return text, False
    guarded = _TEST_CLAIM.sub("[unverified: no passing test run recorded]", text)
    return guarded, True


def _tests_section(ledger: EvidenceLedger) -> list[str]:
    runs = ledger.of("tests")
    if not runs:
        return ["No tests were run for this request."]
    lines = []
    for item in runs:
        phase = {"baseline": "Before changes", "proposed": "With proposed changes (sandbox)",
                 "applied": "After applying changes"}.get(item.data.get("phase", ""), "Test run")
        icon = "✅" if item.status == "passed" else "❌"
        lines.append(f"- {icon} **{phase}:** {item.summary.split(': ', 1)[-1]}")
        for failure in (item.data.get("failures") or [])[:5]:
            lines.append(f"  - `{failure.get('test')}` {failure.get('message', '')[:160]}")
    return lines


def compose_report(*, request: str, primary: dict[str, Any], ledger: EvidenceLedger,
                   citations: list[dict[str, Any]], critic: dict[str, Any] | None, changeset: dict[str, Any] | None,
                   notices: list[str], approval: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    body = str(primary.get("text") or "").strip()
    output = primary.get("output") or {}
    if not body:
        body = str(output.get("answer") or output.get("summary") or primary.get("summary") or "").strip()
    body, guarded = guard_claims(body, ledger)
    if body:
        parts.append(body)
    findings = output.get("findings") or []
    if findings:
        parts.append("### Findings")
        for f in findings[:40]:
            loc = f" — `{f['file']}{':' + str(f['line']) if f.get('line') else ''}`" if f.get("file") else ""
            parts.append(f"- {_SEVERITY_ICON.get(f.get('severity', 'info'), '•')} **{f.get('title')}**{loc}"
                         + (f": {f['detail']}" if f.get("detail") else ""))
        if output.get("recommendations"):
            parts.append("**Recommendations**\n" + "\n".join(f"- {r}" for r in output["recommendations"][:15]))
    if output.get("evidence") and output.get("root_cause"):
        parts.append("**Evidence**\n" + "\n".join(
            f"- `{e.get('file')}{':' + str(e['line']) if e.get('line') else ''}` {e.get('explanation', '')}"
            for e in output["evidence"][:10]))
    if changeset and changeset.get("stats", {}).get("files"):
        stats = changeset["stats"]
        state = {"open": "proposed — **not applied yet**", "applied": "applied", "discarded": "discarded",
                 "conflict": "not applied (files changed since the proposal)"}.get(changeset.get("status", ""), changeset.get("status"))
        parts.append(f"### Changes ({state})")
        for p in stats.get("paths", []):
            parts.append(f"- `{p['path']}` ({p['operation']}, +{p['added']}/-{p['removed']})")
        if approval and approval.get("status") == "pending":
            parts.append("Review the diff and approve to apply these changes.")
    parts.append("### Tests")
    parts.extend(_tests_section(ledger))
    if critic:
        verdict = critic.get("verdict", "uncertain")
        icon = {"pass": "✅", "fail": "❌"}.get(verdict, "⚠️")
        parts.append(f"### Verification\n{icon} Critic verdict: **{verdict}** — {critic.get('summary', '')}")
        for issue in (critic.get("issues") or [])[:8]:
            parts.append(f"- {_SEVERITY_ICON.get(issue.get('severity', 'info'), '•')} ({issue.get('category')}) {issue.get('detail')}")
    if citations:
        cited = set(re.findall(r"\[(S\d+)\]", body))
        parts.append("### Sources" if cited else "### Retrieved context (not cited inline by the model)")
        for c in citations:
            if cited and c["id"] not in cited:
                continue
            symbol = f" — {c['symbol']}" if c.get("symbol") else ""
            parts.append(f"- [{c['id']}] `{c['file_path']}` lines {c['start_line']}-{c['end_line']}{symbol}")
    all_notices = list(dict.fromkeys(notices + (["Unverified test claims were removed from the answer."] if guarded else [])))
    if all_notices:
        parts.append("### Notes\n" + "\n".join(f"- {n}" for n in all_notices[:10]))
    report = {
        "answer": body, "findings": findings[:100], "citations": citations, "tests": [i.model_dump() for i in ledger.of("tests")],
        "verification": critic, "changeset": changeset, "notices": all_notices, "approval": approval,
    }
    return "\n\n".join(parts).strip(), report


class ResponderAgent(BaseAgent):
    """Wraps ``compose_report`` so the responder is visible/configurable like other agents."""

    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        text, report = compose_report(
            request=task.request, primary=task.inputs.get("primary") or {}, ledger=ctx.evidence,
            citations=task.citations, critic=task.inputs.get("critic"), changeset=task.inputs.get("changeset"),
            notices=list(task.inputs.get("notices") or []), approval=task.inputs.get("approval"),
        )
        result.text = text
        result.output = report
        result.summary = text[:200]
