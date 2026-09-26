"""Intent and complexity detection.

Fast path: weighted keyword rules (no model call). Only when the rules are not
confident — and a fast model is healthy — is a small model asked to classify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.json_output import extract_json_object
from app.core.exceptions import ModelError
from app.core.logging import get_logger
from app.models.providers.base import ChatMessage, ChatRequest, ModelRole
from app.models.router import ModelRequirements, ModelRouter

log = get_logger(__name__)

INTENTS = (
    "general", "explain", "search_code", "generate_code", "modify_code", "fix_bug", "debug", "analyze_logs",
    "review", "refactor", "write_tests", "run_tests", "architecture", "documentation", "sql", "git", "docker",
    "devops", "security", "data_analysis",
)

_RULES: tuple[tuple[str, float, re.Pattern[str]], ...] = (
    ("fix_bug", 3.0, re.compile(r"\b(fix|repair|resolve|patch)\b.{0,60}\b(bug|issue|error|problem|failure|failing|crash|exception|broken)\b|\bfix (it|this|the)\b", re.I)),
    ("debug", 2.5, re.compile(r"\b(debug|why (is|does|do|are)\b.{0,40}\b(fail|failing|error|crash|broken|not work)|traceback|stack ?trace|exception|root cause)\b", re.I)),
    ("analyze_logs", 2.5, re.compile(r"\b(analy[sz]e|check|read|look at)\b.{0,20}\blogs?\b|\.log\b", re.I)),
    ("write_tests", 2.8, re.compile(r"\b(write|add|create|generate|increase)\b.{0,30}\b(unit |integration |e2e )?tests?\b|\btest coverage\b", re.I)),
    ("run_tests", 2.6, re.compile(r"\b(run|execute)\b.{0,15}\b(the )?tests?\b", re.I)),
    ("review", 2.5, re.compile(r"\b(code )?review\b|\baudit (the|my) code\b", re.I)),
    ("refactor", 2.6, re.compile(r"\brefactor|clean ?up the code|restructure|simplify (the|this) (code|function|module)", re.I)),
    ("security", 2.8, re.compile(r"\b(security|vulnerab\w*|secrets?|injection|xss|csrf|owasp|cve|hardcoded (password|key|token))\b", re.I)),
    ("sql", 2.6, re.compile(r"\b(sql|query|queries|select .* from|database schema|join|table)\b", re.I)),
    ("git", 2.6, re.compile(r"\b(git|commit|branch|merge|rebase|diff|stash|checkout)\b", re.I)),
    ("docker", 2.5, re.compile(r"\b(docker|dockerfile|container|compose|image)\b", re.I)),
    ("devops", 2.3, re.compile(r"\b(ci/?cd|pipeline|deploy\w*|kubernetes|k8s|helm|terraform|monitoring|prometheus|github actions)\b", re.I)),
    ("architecture", 2.4, re.compile(r"\b(architecture|design|system design|implementation plan|how is (the|this) project (structured|organized)|components|scalab\w+)\b", re.I)),
    ("documentation", 2.4, re.compile(r"\b(document|documentation|docs|readme|docstrings?|api reference)\b", re.I)),
    ("data_analysis", 2.3, re.compile(r"\b(analy[sz]e (the )?data|csv|dataset|statistics|plot|dataframe|pandas)\b", re.I)),
    ("generate_code", 2.0, re.compile(r"\b(write|create|generate|implement|build|add)\b.{0,40}\b(function|class|endpoint|api|script|module|feature|component|code|program)\b", re.I)),
    ("modify_code", 2.0, re.compile(r"\b(change|modify|update|rename|replace|move|extend|improve)\b.{0,40}\b(function|class|code|file|module|endpoint|method)\b", re.I)),
    ("search_code", 2.0, re.compile(r"\b(where is|find|locate|search|which file|show me)\b", re.I)),
    ("explain", 1.6, re.compile(r"\b(explain|how does|how do|what does|what is|describe|walk me through|understand|overview)\b", re.I)),
)

PROJECT_INTENTS = {"explain", "search_code", "fix_bug", "debug", "analyze_logs", "review", "refactor", "write_tests",
                   "run_tests", "architecture", "documentation", "git", "docker", "devops", "security", "modify_code"}


@dataclass
class IntentResult:
    intent: str
    complexity: str
    confidence: float
    source: str  # rules | model | default

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def estimate_complexity(text: str) -> str:
    words = len(text.split())
    lowered = text.lower()
    if words <= 4 and re.fullmatch(r"(hi|hello|hey|thanks|thank you|ok|okay)[!. ]*", lowered.strip()):
        return "trivial"
    score = 0
    score += 2 if words > 120 else 1 if words > 40 else 0
    score += 1 if len(re.findall(r"\b(and then|also|additionally|after that|then)\b", lowered)) >= 2 else 0
    score += 1 if re.search(r"\b(entire|whole|all (files|modules)|across the (project|codebase)|end[- ]to[- ]end)\b", lowered) else 0
    score += 1 if re.search(r"\b(architecture|design|migrate|refactor|security|performance|scal\w+|concurren\w+)\b", lowered) else 0
    score += 1 if lowered.count("?") > 2 or lowered.count("\n") > 8 else 0
    return "complex" if score >= 3 else "moderate" if score >= 1 else "simple"


def rule_intent(text: str, has_project: bool) -> tuple[str, float]:
    scores: dict[str, float] = {}
    for intent, weight, pattern in _RULES:
        matches = len(pattern.findall(text))
        if matches:
            scores[intent] = scores.get(intent, 0.0) + weight + 0.3 * (matches - 1)
    if not scores:
        return ("explain" if has_project else "general"), 0.3
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = min(1.0, 0.45 + (best_score - runner_up) / 4 + (0.15 if best_score >= 2.5 else 0))
    return best, round(confidence, 2)


async def detect_intent(text: str, *, has_project: bool, router: ModelRouter | None) -> IntentResult:
    intent, confidence = rule_intent(text, has_project)
    complexity = estimate_complexity(text)
    if confidence >= 0.6 or router is None:
        return IntentResult(intent, complexity, confidence, "rules")
    try:
        routed = await router.chat(
            ChatRequest(messages=[
                ChatMessage(role="system", content=(
                    "Classify the developer request. Reply with JSON {\"intent\": <one of: " + ", ".join(INTENTS)
                    + ">, \"complexity\": trivial|simple|moderate|complex}.")),
                ChatMessage(role="user", content=text[:2000]),
            ], json_mode=True, max_tokens=60, temperature=0.0),
            ModelRequirements(role=ModelRole.FAST, complexity="trivial", latency_sensitive=True, agent_id="intent"),
        )
        data = extract_json_object(routed.response.content) or {}
        model_intent = str(data.get("intent", "")).strip()
        if model_intent in INTENTS:
            model_complexity = data.get("complexity") if data.get("complexity") in (
                "trivial", "simple", "moderate", "complex") else complexity
            return IntentResult(model_intent, str(model_complexity), 0.7, "model")
    except ModelError as exc:
        log.info("intent.model_unavailable", code=exc.code)
    return IntentResult(intent, complexity, confidence, "rules")
