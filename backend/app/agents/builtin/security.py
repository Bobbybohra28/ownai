"""Security agent.

Deterministic scanners produce evidence-backed findings (file + line) for hardcoded
secrets, dangerous commands, injection patterns, unsafe deserialisation, insecure
configuration and unpinned dependencies. A model then triages and explains them.
Findings always originate from the scanners, never from the model alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent
from app.agents.runtimes.single_shot import output_instructions, structured_call
from app.agents.schemas import AnalysisReport, Finding
from app.core.exceptions import AgentError, AppError, ErrorCode
from app.models.providers.base import ChatMessage
from app.projects.scanner import scan_project
from app.security.commands import find_dangerous_patterns
from app.security.secrets import find_secrets, mask_secrets


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    pattern: re.Pattern[str]
    languages: frozenset[str] | None
    recommendation: str


RULES: tuple[Rule, ...] = (
    Rule("sql_injection_fstring", "high", "Possible SQL injection (string-built query)",
         re.compile(r"""(?i)(execute|executemany|raw|query)\(\s*(f["']|["'][^"']*["']\s*(%|\+|\.format))[^)]*\b(select|insert|update|delete|where)\b"""),
         frozenset({"python"}), "Use parameterised queries (placeholders) instead of string formatting."),
    Rule("sql_injection_js", "high", "Possible SQL injection (template literal query)",
         re.compile(r"""(?i)\.(query|execute|raw)\(\s*`[^`]*\$\{[^`]*\b(select|insert|update|delete|where)\b"""),
         frozenset({"javascript", "typescript", "jsx", "tsx"}), "Use parameterised queries."),
    Rule("command_injection", "high", "Shell command built from variables",
         re.compile(r"""subprocess\.\w+\([^)]*shell\s*=\s*True|os\.system\(\s*f?["'][^"']*(\{|%|\+)|os\.popen\("""),
         frozenset({"python"}), "Pass an argument list to subprocess without shell=True."),
    Rule("command_injection_js", "high", "Shell command built from variables",
         re.compile(r"""child_process|\bexec(Sync)?\(\s*`[^`]*\$\{"""), frozenset({"javascript", "typescript"}),
         "Use execFile/spawn with an argument array; never interpolate user input."),
    Rule("eval", "high", "Dynamic code execution (eval/exec)",
         re.compile(r"(?<![\w.])(eval|exec)\s*\("), frozenset({"python", "javascript", "typescript"}),
         "Avoid eval/exec on data; use safe parsers."),
    Rule("pickle", "high", "Unsafe deserialisation", re.compile(r"\bpickle\.loads?\(|\byaml\.load\((?![^)]*SafeLoader)"),
         frozenset({"python"}), "Use json or yaml.safe_load; never unpickle untrusted data."),
    Rule("path_traversal", "medium", "File path built from request data",
         re.compile(r"""open\(\s*(request\.|req\.|params|f["'][^"']*\{(request|req|params|user))|os\.path\.join\([^)]*(request\.|req\.)"""),
         frozenset({"python"}), "Resolve the path and verify it stays inside an allowed directory."),
    Rule("path_traversal_js", "medium", "File path built from request data",
         re.compile(r"""(readFile|createReadStream|sendFile)\([^)]*req\.(params|query|body)"""),
         frozenset({"javascript", "typescript"}), "Normalise the path and check it against an allowed root."),
    Rule("weak_hash", "medium", "Weak hash for passwords/tokens",
         re.compile(r"(?i)(md5|sha1)\s*\(.*(pass|pwd|token|secret)|hashlib\.(md5|sha1)\("),
         frozenset({"python", "javascript", "typescript", "java", "go"}), "Use argon2/bcrypt/scrypt/PBKDF2 for passwords."),
    Rule("jwt_no_verify", "high", "JWT signature verification disabled",
         re.compile(r"""verify_signature["']?\s*:\s*False|algorithms\s*=\s*\[\s*["']none["']|jwt\.decode\([^)]*verify\s*=\s*False"""),
         None, "Always verify JWT signatures with an explicit algorithm list."),
    Rule("tls_verify_off", "high", "TLS certificate verification disabled",
         re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true"),
         None, "Keep certificate verification enabled."),
    Rule("debug_enabled", "medium", "Debug mode enabled", re.compile(r"(?i)\bdebug\s*=\s*true\b|app\.run\([^)]*debug\s*=\s*True"),
         None, "Disable debug mode outside development."),
    Rule("cors_wildcard", "medium", "CORS allows any origin",
         re.compile(r"""allow_origins\s*=\s*\[\s*["']\*["']|Access-Control-Allow-Origin["']?\s*[:,]\s*["']\*"""),
         None, "Restrict CORS to known origins, especially with credentials."),
    Rule("plain_compare_secret", "low", "Non-constant-time secret comparison",
         re.compile(r"(?i)(token|signature|password|secret|digest)\w*\s*==\s*\w*(token|signature|password|secret|digest)"),
         frozenset({"python"}), "Use hmac.compare_digest for secrets."),
    Rule("bind_all", "low", "Service binds to all interfaces", re.compile(r"""host\s*=\s*["']0\.0\.0\.0["']"""),
         None, "Bind to localhost unless external access is intended (and protected)."),
)


def scan_findings(root: Path, *, max_findings: int = 200) -> list[Finding]:
    scan = scan_project(root, max_files=5000, max_file_kb=400)
    findings: list[Finding] = []
    for f in scan.files:
        if f.is_sensitive:
            findings.append(Finding(severity="medium", title="Credential file present in project", file=f.path,
                                    detail="This file matches a credential pattern. Ensure it is git-ignored and not deployed.",
                                    recommendation="Keep secrets out of the repository; use a secret manager."))
            continue
        if f.language is None:
            continue
        try:
            text = (root / f.path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for secret in find_secrets(text):
            findings.append(Finding(severity="critical" if secret.rule != "assignment" else "high",
                                    title=f"Hardcoded secret ({secret.rule})", file=f.path, line=secret.line,
                                    detail=f"Value {secret.preview} appears to be a credential.",
                                    recommendation="Move it to an environment variable/secret store and rotate it."))
        if f.is_test:
            continue
        lines = text.split("\n")
        for rule in RULES:
            if rule.languages is not None and f.language not in rule.languages:
                continue
            for idx, line in enumerate(lines, start=1):
                if rule.pattern.search(line):
                    findings.append(Finding(severity=rule.severity, title=rule.title, file=f.path, line=idx,  # type: ignore[arg-type]
                                            detail=mask_secrets(line.strip())[:200], recommendation=rule.recommendation))
        if f.language in {"shell", "powershell", "dockerfile", "yaml", "makefile"} or f.path.endswith((".sh", ".yml", ".yaml")):
            for danger in find_dangerous_patterns(text):
                line_no = text[: text.find(danger.match)].count("\n") + 1 if danger.match in text else None
                findings.append(Finding(severity="medium", title=f"Dangerous command: {danger.description}",
                                        file=f.path, line=line_no, detail=danger.match))
        if len(findings) >= max_findings:
            break
    return findings[:max_findings]


def dependency_findings(overview: dict) -> list[Finding]:
    findings: list[Finding] = []
    for dep in overview.get("dependencies", []):
        if not dep.get("dev") and not dep.get("pinned"):
            findings.append(Finding(severity="low", title=f"Unpinned dependency: {dep['name']}", file=dep.get("manifest"),
                                    detail=f"Version spec '{dep.get('version') or 'any'}' allows unreviewed upgrades.",
                                    recommendation="Pin versions (lock file) and scan them with a vulnerability database."))
    return findings[:50]


_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class SecurityAgent(BaseAgent):
    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        if ctx.project is None:
            raise AgentError("Security analysis needs a project.", code=ErrorCode.INSUFFICIENT_CONTEXT)
        await ctx.status("Scanning for security issues…")
        findings = scan_findings(ctx.project.root) + dependency_findings(ctx.project.overview or {})
        findings.sort(key=lambda f: (_ORDER.get(f.severity, 5), f.file or "", f.line or 0))
        counts: dict[str, int] = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        summary = (", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: _ORDER[kv[0]]))
                   or "no issues detected by the scanners")
        report = AnalysisReport(summary=f"Security scan: {summary}.", findings=findings,
                                recommendations=[], citations=[])
        if findings:
            await ctx.status("Triaging findings…")
            listing = "\n".join(f"- [{f.severity}] {f.title} at {f.file}:{f.line or '?'} — {f.detail}" for f in findings[:40])
            messages = [
                ChatMessage(role="system", content=self.system_prompt(
                    "You triage security scanner findings. Do not add findings that are not in the list. "
                    "Summarise the most important risks and give prioritised, concrete recommendations. "
                    + output_instructions(AnalysisReport))),
                ChatMessage(role="user", content=f"Request: {task.request}\n\nScanner findings:\n{listing}"
                            + (f"\n\n<context>\n{task.context[:6000]}\n</context>" if task.context else "")),
            ]
            try:
                triage = await structured_call(self, ctx, task, messages, AnalysisReport, result)
                report.summary = triage.summary or report.summary
                report.recommendations = triage.recommendations
            except AppError as exc:
                result.notices.append(f"Model triage unavailable ({exc.message}); showing raw scanner findings.")
        result.output = report.model_dump()
        result.summary = report.summary
        result.text = report.summary
