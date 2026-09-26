"""Docker, process and log inspection tools (read-only)."""

from __future__ import annotations

import re
from collections import deque

import yaml
from pydantic import Field

from app.core.exceptions import ErrorCode, ToolError
from app.security.secrets import is_sensitive_key, mask_secrets
from app.tools.base import PermissionLevel, Tool, ToolArgs, ToolContext, ToolOutcome

_LOG_PATH = re.compile(r"(^|/)(logs?/|[^/]+\.log(\.\d+)?$|[^/]+\.out$)")


class NoArgs(ToolArgs):
    pass


def _compose_summary(text: str) -> dict:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return {"error": f"invalid YAML: {str(exc).splitlines()[0]}"}
    services = {}
    for name, svc in (data.get("services") or {}).items():
        svc = svc or {}
        env = svc.get("environment") or {}
        env_names = list(env.keys()) if isinstance(env, dict) else [str(e).split("=", 1)[0] for e in env]
        services[name] = {
            "image": svc.get("image"), "build": bool(svc.get("build")), "ports": svc.get("ports", []),
            "depends_on": list(svc.get("depends_on") or []), "healthcheck": bool(svc.get("healthcheck")),
            "volumes": svc.get("volumes", []), "environment": env_names,
            "privileged": bool(svc.get("privileged")), "network_mode": svc.get("network_mode"),
            "hardcoded_secrets": [k for k, v in (env.items() if isinstance(env, dict) else [])
                                  if is_sensitive_key(k) and v and not str(v).startswith("${")],
        }
    return {"services": services}


class DockerStatusTool(Tool):
    name = "docker_status"
    description = ("Summarise the project's Docker setup (Dockerfiles, compose services, ports, health checks) and "
                   "list containers OwnAI started for this project.")
    args_model = NoArgs
    timeout_s = 60

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        root = project.root
        dockerfiles = [p.relative_to(root).as_posix() for p in root.rglob("Dockerfile*")
                       if "node_modules" not in p.parts][:20]
        compose: dict[str, dict] = {}
        for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml"):
            for p in root.rglob(pattern):
                if "node_modules" in p.parts:
                    continue
                compose[p.relative_to(root).as_posix()] = _compose_summary(p.read_text(encoding="utf-8", errors="ignore"))
        containers: list[dict] | str
        try:
            containers = await ctx.services.sandbox.containers(project.label)
        except Exception as exc:  # sandbox optional for static analysis
            containers = f"unavailable: {getattr(exc, 'message', str(exc))}"
        return ToolOutcome(status="ok", summary=f"{len(dockerfiles)} Dockerfile(s), {len(compose)} compose file(s)",
                           data={"dockerfiles": dockerfiles, "compose": compose, "managed_containers": containers})


class DockerLogsArgs(ToolArgs):
    container: str = Field(description="Name of a container started by OwnAI for this project")
    tail: int = Field(default=200, ge=1, le=2000)


class DockerLogsTool(Tool):
    name = "docker_logs"
    description = "Read logs of a container OwnAI started for this project."
    args_model = DockerLogsArgs
    timeout_s = 60

    async def run(self, args: DockerLogsArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        data = await ctx.services.sandbox.logs(args.container, project.label, args.tail)
        return ToolOutcome(status="ok", summary=f"Last {args.tail} log lines of {args.container}",
                           data={"logs": mask_secrets(data.get("logs", "")), "truncated": data.get("truncated")})


class InspectProcessTool(Tool):
    name = "inspect_process"
    description = "List sandbox executions/containers currently running for this project."
    args_model = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        containers = await ctx.services.sandbox.containers(project.label)
        running = [c for c in containers if (c.get("State") or "").lower() == "running"]
        return ToolOutcome(status="ok", summary=f"{len(running)} running", data={"running": running})


class ReadLogsArgs(ToolArgs):
    path: str = Field(description="Log file inside the project (e.g. logs/app.log)")
    tail: int = Field(default=200, ge=1, le=5000)
    grep: str | None = Field(default=None, max_length=200)


class ReadLogsTool(Tool):
    name = "read_logs"
    description = "Read the tail of a log file in the project, optionally filtered by a regular expression."
    args_model = ReadLogsArgs
    permission = PermissionLevel.READ

    async def run(self, args: ReadLogsArgs, ctx: ToolContext) -> ToolOutcome:
        project = ctx.require_project()
        rel = project.jail.normalize(args.path)
        if not _LOG_PATH.search(rel):
            raise ToolError("read_logs only reads *.log files or files under logs/ directories; use read_file otherwise.",
                            code=ErrorCode.PATH_NOT_ALLOWED)
        target = project.jail.resolve(rel, must_exist=True)
        pattern = None
        if args.grep:
            try:
                pattern = re.compile(args.grep, re.IGNORECASE)
            except re.error as exc:
                raise ToolError(f"Invalid pattern: {exc}", code=ErrorCode.TOOL_INVALID_ARGS) from exc
        lines: deque[str] = deque(maxlen=args.tail)
        total = 0
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                total += 1
                if pattern is None or pattern.search(line):
                    lines.append(line.rstrip("\n"))
        errors = sum(1 for ln in lines if re.search(r"\b(ERROR|CRITICAL|FATAL|Traceback|Exception)\b", ln))
        return ToolOutcome(status="ok", summary=f"{len(lines)} line(s) from {rel} ({errors} error-like)",
                           data={"path": rel, "lines": mask_secrets("\n".join(lines)), "total_lines": total,
                                 "error_lines": errors})
