"""Tool registry: the single catalogue of tools available to agents and the API."""

from __future__ import annotations

from app.core.exceptions import ErrorCode, ToolError
from app.models.providers.base import ToolSchema
from app.tools.base import Tool
from app.tools.changeset import (
    ApplyChangeSetTool,
    CreateChangeSetTool,
    DeleteFileTool,
    EditFileTool,
    WriteFileTool,
)
from app.tools.code_search import SearchCodeTool, SemanticSearchTool
from app.tools.execution import RunLintTool, RunPythonTool, RunTestsTool
from app.tools.fs import ProjectStructureTool, ReadFileTool, ScanProjectTool
from app.tools.git import (
    GitBranchTool,
    GitCheckoutTool,
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
)
from app.tools.ops import DockerLogsTool, DockerStatusTool, InspectProcessTool, ReadLogsTool
from app.tools.sql import InspectDatabaseTool, RunSQLTool, ValidateSQLTool

BUILTIN_TOOLS: tuple[type[Tool], ...] = (
    ReadFileTool, ProjectStructureTool, ScanProjectTool, SearchCodeTool, SemanticSearchTool,
    WriteFileTool, EditFileTool, DeleteFileTool, CreateChangeSetTool, ApplyChangeSetTool,
    RunPythonTool, RunTestsTool, RunLintTool,
    RunSQLTool, InspectDatabaseTool, ValidateSQLTool,
    GitStatusTool, GitDiffTool, GitLogTool, GitBranchTool, GitCheckoutTool, GitCommitTool,
    DockerStatusTool, DockerLogsTool, InspectProcessTool, ReadLogsTool,
)


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools if tools is not None else [cls() for cls in BUILTIN_TOOLS]:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name '{tool.name}'")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool '{name}'.", code=ErrorCode.TOOL_NOT_FOUND)
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self, names: list[str] | None = None) -> list[ToolSchema]:
        tools = [self._tools[n] for n in names if n in self._tools] if names is not None else self.all()
        return [ToolSchema(name=t.name, description=t.description, parameters=t.json_schema()) for t in tools]

    def describe(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "permission": t.permission.label,
                 "timeout_s": t.timeout_s, "requires_project": t.requires_project, "schema": t.json_schema()}
                for t in self.all()]
