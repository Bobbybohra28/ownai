"""Structured output schemas agents must produce. Validated with Pydantic."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low", "info"]


class Finding(BaseModel):
    severity: Severity = "medium"
    title: str
    detail: str = ""
    file: str | None = None
    line: int | None = None
    recommendation: str | None = None


class AnswerOutput(BaseModel):
    answer: str = Field(description="Markdown answer. Cite sources inline as [S1], [S2].")
    citations: list[str] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    file: str
    line: int | None = None
    explanation: str = ""


class DebugReport(BaseModel):
    summary: str
    root_cause: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"


class FileEdit(BaseModel):
    path: str
    action: Literal["replace", "create", "delete", "rewrite"] = "replace"
    find: str | None = Field(default=None, description="Exact existing text to replace (action=replace)")
    replace: str | None = Field(default=None, description="Replacement text (action=replace)")
    content: str | None = Field(default=None, description="Full file content (action=create/rewrite)")


class CodeChangeProposal(BaseModel):
    summary: str
    edits: list[FileEdit] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CriticIssue(BaseModel):
    severity: Severity = "medium"
    category: Literal["correctness", "requirements", "security", "edge_case", "syntax", "tests", "compatibility",
                      "hallucination", "unsupported_claim", "other"] = "other"
    detail: str


class CriticVerdict(BaseModel):
    verdict: Literal["pass", "fail", "uncertain"]
    issues: list[CriticIssue] = Field(default_factory=list)
    unmet_requirements: list[str] = Field(default_factory=list)
    summary: str = ""


class PlanStepOut(BaseModel):
    id: str
    agent: str
    goal: str
    depends_on: list[str] = Field(default_factory=list)


class PlanOutput(BaseModel):
    steps: list[PlanStepOut]
    success_criteria: list[str] = Field(default_factory=list)


class SQLOutput(BaseModel):
    sql: str
    dialect: str = "postgresql"
    explanation: str = ""


class DocumentationOutput(BaseModel):
    title: str
    content: str
    target_path: str | None = None


class IntentOutput(BaseModel):
    intent: str
    complexity: Literal["trivial", "simple", "moderate", "complex"] = "simple"


OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "AnswerOutput": AnswerOutput,
    "AnalysisReport": AnalysisReport,
    "DebugReport": DebugReport,
    "CodeChangeProposal": CodeChangeProposal,
    "CriticVerdict": CriticVerdict,
    "PlanOutput": PlanOutput,
    "SQLOutput": SQLOutput,
    "DocumentationOutput": DocumentationOutput,
    "IntentOutput": IntentOutput,
}
