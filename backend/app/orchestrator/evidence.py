"""Evidence ledger: an append-only record of what actually happened during a run.

Final reports are rendered from this ledger. A claim such as "tests passed" can only
appear when a test tool run with a passing result is recorded here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["retrieval", "tool_run", "tests", "lint", "syntax", "changeset", "apply", "critic",
                       "model_call", "approval", "notice"]


class EvidenceItem(BaseModel):
    kind: EvidenceKind
    status: Literal["passed", "failed", "info", "skipped", "error"] = "info"
    summary: str
    step: str | None = None
    agent: str | None = None
    ref: str | None = None  # tool_run id / changeset id / approval id
    data: dict[str, Any] = Field(default_factory=dict)
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class EvidenceLedger(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)

    def add(self, item: EvidenceItem) -> EvidenceItem:
        self.items.append(item)
        return item

    def of(self, *kinds: EvidenceKind) -> list[EvidenceItem]:
        return [i for i in self.items if i.kind in kinds]

    def latest(self, kind: EvidenceKind, *, where: dict[str, Any] | None = None) -> EvidenceItem | None:
        for item in reversed(self.items):
            if item.kind == kind and all(item.data.get(k) == v for k, v in (where or {}).items()):
                return item
        return None

    def tests_passed(self, *, applied: bool | None = None) -> bool | None:
        """True/False if tests ran (optionally before/after applying), None if no test run exists."""
        runs = [i for i in self.of("tests") if applied is None or i.data.get("applied") == applied]
        if not runs:
            return None
        return runs[-1].status == "passed"

    def summary_for_prompt(self, limit: int = 20) -> str:
        lines = []
        for item in self.items[-limit:]:
            lines.append(f"- [{item.kind}/{item.status}] {item.summary}")
        return "\n".join(lines) or "(no evidence recorded)"
