"""Tool interface.

Every tool declares: a Pydantic input schema, a permission level, a timeout, and an
approval rule. Tools never execute directly — ``ToolExecutor`` validates, authorizes,
routes risky calls to human approval, enforces the timeout and writes the audit trail.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.database.models import OrgRole
from app.security.paths import PathJail

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import Settings
    from app.rag.retrieval import HybridRetriever
    from app.sandbox.client import SandboxClient
    from app.security.crypto import SecretBox
    from app.tools.changeset import ChangeSetService


class PermissionLevel(IntEnum):
    READ = 0           # read files, search, git status/diff/log, inspect
    WRITE_STAGED = 1   # stage edits in a reviewable change set (reversible, not applied)
    EXECUTE = 2        # run code/tests/linters inside the sandbox
    DESTRUCTIVE = 3    # modifies real state: apply changes, write SQL, git commit/checkout
    ADMIN = 4          # credentials, security settings, infrastructure

    @property
    def label(self) -> str:
        return self.name.lower()


ROLE_MAX_PERMISSION: dict[OrgRole, PermissionLevel] = {
    OrgRole.VIEWER: PermissionLevel.READ,
    OrgRole.MEMBER: PermissionLevel.DESTRUCTIVE,  # destructive still requires explicit approval
    OrgRole.ADMIN: PermissionLevel.ADMIN,
    OrgRole.OWNER: PermissionLevel.ADMIN,
}


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ProjectRef:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    root: Path
    overview: dict[str, Any] = field(default_factory=dict)

    @property
    def jail(self) -> PathJail:
        return PathJail(self.root)

    @property
    def label(self) -> str:
        return str(self.id)


@dataclass
class ToolServices:
    sessions: async_sessionmaker[AsyncSession]
    settings: Settings
    sandbox: SandboxClient
    changesets: ChangeSetService
    retriever: HybridRetriever | None = None
    secret_box: SecretBox | None = None


@dataclass
class ToolContext:
    org_id: uuid.UUID
    user_id: uuid.UUID | None
    role: OrgRole
    services: ToolServices
    project: ProjectRef | None = None
    run_id: uuid.UUID | None = None
    step_key: str | None = None
    agent_id: str | None = None
    approved: bool = False          # set when executing an action a human approved
    approval_id: uuid.UUID | None = None

    def require_project(self) -> ProjectRef:
        if self.project is None:
            from app.core.exceptions import ErrorCode, ToolError

            raise ToolError("This action needs a project. Select or import a project first.",
                            code=ErrorCode.INSUFFICIENT_CONTEXT)
        return self.project


class ToolOutcome(BaseModel):
    status: Literal["ok", "error", "approval_required", "denied"]
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    approval_id: str | None = None
    tool_run_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def for_model(self, max_chars: int = 6000) -> str:
        """Compact text shown to the LLM as the tool result."""
        import json

        body: dict[str, Any] = {"status": self.status, "summary": self.summary}
        if self.data:
            body["data"] = self.data
        if self.error_code:
            body["error_code"] = self.error_code
        text = json.dumps(body, ensure_ascii=False, default=str)
        return text if len(text) <= max_chars else text[:max_chars] + '…"}'


@dataclass
class ApprovalRequest:
    title: str
    description: str
    risk_level: Literal["medium", "high", "critical"] = "high"


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[ToolArgs]]
    permission: ClassVar[PermissionLevel] = PermissionLevel.READ
    timeout_s: ClassVar[int] = 30
    requires_project: ClassVar[bool] = True

    def approval_needed(self, args: ToolArgs, ctx: ToolContext) -> ApprovalRequest | None:
        """Return an ApprovalRequest when this particular call needs human approval."""
        if self.permission >= PermissionLevel.DESTRUCTIVE:
            return ApprovalRequest(title=f"Run {self.name}", description=f"{self.name} changes real state.")
        return None

    @abstractmethod
    async def run(self, args: Any, ctx: ToolContext) -> ToolOutcome: ...

    def json_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return schema
