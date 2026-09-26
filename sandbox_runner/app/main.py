"""OwnAI sandbox runner.

A small, separately deployed FastAPI service and the ONLY OwnAI component with access
to the container runtime. The API/worker send a workspace archive plus an argv list;
the runner executes it in a throw-away container with:

* no network (``--network none``), read-only root filesystem, workspace on tmpfs
* all Linux capabilities dropped, ``no-new-privileges``, non-root user
* CPU, memory (no swap), PID and wall-clock limits; output size limits
* optional gVisor runtime (``SANDBOX_RUNTIME=runsc``)

The workspace is streamed into the container over stdin (``tar -x``), so no host
paths are ever bind-mounted.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
import uuid
from pathlib import PurePosixPath
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, ValidationError

DOCKER = os.environ.get("SANDBOX_DOCKER_BIN", "docker")
TOKEN = os.environ.get("SANDBOX_TOKEN", "")
RUNTIME = os.environ.get("SANDBOX_RUNTIME", "")  # e.g. "runsc" for gVisor
MAX_TIMEOUT_S = int(os.environ.get("SANDBOX_MAX_TIMEOUT_S", "900"))
DEFAULT_MEMORY_MB = int(os.environ.get("SANDBOX_MEMORY_MB", "1024"))
MAX_MEMORY_MB = int(os.environ.get("SANDBOX_MAX_MEMORY_MB", "4096"))
DEFAULT_CPUS = float(os.environ.get("SANDBOX_CPUS", "1.0"))
PIDS_LIMIT = int(os.environ.get("SANDBOX_PIDS_LIMIT", "256"))
WORKSPACE_TMPFS = os.environ.get("SANDBOX_WORKSPACE_SIZE", "1g")
OUTPUT_LIMIT = int(os.environ.get("SANDBOX_OUTPUT_LIMIT_BYTES", str(256 * 1024)))
MAX_ARCHIVE_BYTES = int(os.environ.get("SANDBOX_MAX_ARCHIVE_MB", "200")) * 1024 * 1024
MAX_CONCURRENT = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "4"))
ENVIRONMENT = os.environ.get("SANDBOX_ENV", "development")

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "python": {"image": "ownai/sandbox-python:3.12", "allow": ["python", "python3", "pytest", "ruff", "pip"]},
    "node": {"image": "node:22-slim", "allow": ["node", "npm"]},
    "go": {"image": "golang:1.23", "allow": ["go"]},
    "rust": {"image": "rust:1-slim", "allow": ["cargo"]},
}
PROFILES: dict[str, dict[str, Any]] = {**DEFAULT_PROFILES, **json.loads(os.environ.get("SANDBOX_PROFILES_JSON", "{}"))}
_DENIED = {("pip", "install"), ("pip", "uninstall"), ("pip", "download"), ("npm", "install"), ("npm", "i"),
           ("npm", "publish"), ("npm", "exec"), ("go", "get"), ("go", "install"), ("cargo", "install"),
           ("cargo", "publish")}
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")

app = FastAPI(title="OwnAI Sandbox Runner", version="0.1.0")
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


class ExecutionSpec(BaseModel):
    profile: str
    argv: list[str] = Field(min_length=1, max_length=64)
    timeout_s: int = Field(default=120, ge=1)
    memory_mb: int = Field(default=DEFAULT_MEMORY_MB, ge=64)
    cpus: float = Field(default=DEFAULT_CPUS, gt=0, le=8)
    workdir: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    project: str = "none"
    execution_id: str | None = None


class ExecutionResult(BaseModel):
    execution_id: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    oom_killed: bool
    truncated: bool
    image: str
    started: bool
    error: str | None = None


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not TOKEN:
        if ENVIRONMENT == "production":
            raise HTTPException(503, "Sandbox runner has no SANDBOX_TOKEN configured.")
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, TOKEN):
        raise HTTPException(401, "Invalid sandbox token.")


async def _run(*args: str, stdin: bytes | None = None, timeout: float = 30) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


def _truncate(data: bytes) -> tuple[str, bool]:
    if len(data) <= OUTPUT_LIMIT:
        return data.decode("utf-8", errors="replace"), False
    half = OUTPUT_LIMIT // 2
    return (data[:half].decode("utf-8", errors="replace") + "\n…[output truncated]…\n"
            + data[-half:].decode("utf-8", errors="replace")), True


def validate_spec(spec: ExecutionSpec) -> dict[str, Any]:
    profile = PROFILES.get(spec.profile)
    if profile is None:
        raise HTTPException(400, f"Unknown profile '{spec.profile}'.")
    exe = PurePosixPath(spec.argv[0]).name
    if exe not in profile["allow"]:
        raise HTTPException(403, f"'{exe}' is not allowed in the {spec.profile} sandbox.")
    if len(spec.argv) > 1 and (exe, spec.argv[1]) in _DENIED:
        raise HTTPException(403, f"'{exe} {spec.argv[1]}' is not allowed in the sandbox.")
    if any("\x00" in a or len(a) > 8192 for a in spec.argv):
        raise HTTPException(400, "Invalid argument.")
    workdir = PurePosixPath(spec.workdir or ".")
    if workdir.is_absolute() or ".." in workdir.parts:
        raise HTTPException(400, "Invalid workdir.")
    for name in spec.env:
        if not _ENV_NAME.match(name):
            raise HTTPException(400, f"Invalid environment variable name '{name}'.")
    if not _LABEL.match(spec.project):
        raise HTTPException(400, "Invalid project label.")
    return profile


async def execute(spec: ExecutionSpec, archive: bytes | None) -> ExecutionResult:
    profile = validate_spec(spec)
    execution_id = spec.execution_id if spec.execution_id and _LABEL.match(spec.execution_id) else uuid.uuid4().hex
    name = f"ownai-exec-{execution_id}"
    timeout = min(spec.timeout_s, MAX_TIMEOUT_S)
    memory = min(spec.memory_mb, MAX_MEMORY_MB)
    workdir = "/workspace/" + PurePosixPath(spec.workdir or ".").as_posix().lstrip("./")
    docker_args = [
        DOCKER, "run", "-i", "--name", name,
        "--label", "ownai.managed=true", "--label", f"ownai.project={spec.project}",
        "--label", f"ownai.execution={execution_id}",
        "--network", "none", "--read-only",
        "--tmpfs", f"/workspace:rw,exec,size={WORKSPACE_TMPFS},uid=1000,gid=1000,mode=0755",
        "--tmpfs", "/tmp:rw,exec,size=256m,uid=1000,gid=1000,mode=1777",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", str(PIDS_LIMIT), "--memory", f"{memory}m", "--memory-swap", f"{memory}m",
        "--cpus", str(spec.cpus), "--user", "1000:1000",
        "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONUNBUFFERED=1", "-e", "CI=1",
        "-e", "npm_config_cache=/tmp/.npm", "-e", "GOCACHE=/tmp/.gocache", "-e", "CARGO_HOME=/tmp/.cargo",
    ]
    for key, value in spec.env.items():
        docker_args += ["-e", f"{key}={value}"]
    if RUNTIME:
        docker_args += ["--runtime", RUNTIME]
    docker_args += ["--workdir", "/workspace", "--entrypoint", "/bin/sh", profile["image"], "-c",
                    'tar -xzf - -C /workspace 2>/dev/null || true; cd "$0" && exec "$@"', workdir, *spec.argv]
    started = time.perf_counter()
    timed_out = False
    exit_code: int | None = None
    stdout = stderr = b""
    async with _semaphore:
        try:
            exit_code, stdout, stderr = await _run(*docker_args, stdin=archive or _empty_tar(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await _run(DOCKER, "kill", name, timeout=30)
        except FileNotFoundError:
            return ExecutionResult(execution_id=execution_id, exit_code=None, stdout="", stderr="", duration_ms=0,
                                   timed_out=False, oom_killed=False, truncated=False, image=profile["image"],
                                   started=False, error="Container runtime (docker) is not installed on the runner.")
        duration_ms = int((time.perf_counter() - started) * 1000)
        oom = False
        try:
            code, out, _ = await _run(DOCKER, "inspect", name, "--format", "{{.State.ExitCode}} {{.State.OOMKilled}}")
            if code == 0:
                parts = out.decode().split()
                if exit_code is None and parts:
                    exit_code = int(parts[0])
                oom = len(parts) > 1 and parts[1] == "true"
        finally:
            await _run(DOCKER, "rm", "-f", name, timeout=60)
    container_started = not (exit_code == 125 and b"docker:" in stderr)
    out_text, t1 = _truncate(stdout)
    err_text, t2 = _truncate(stderr)
    error = None
    if not container_started:
        error = "The sandbox container could not start: " + stderr.decode(errors="replace").strip()[:500]
    return ExecutionResult(execution_id=execution_id, exit_code=exit_code, stdout=out_text, stderr=err_text,
                           duration_ms=duration_ms, timed_out=timed_out, oom_killed=oom, truncated=t1 or t2,
                           image=profile["image"], started=container_started, error=error)


def _empty_tar() -> bytes:
    import gzip
    import io
    import tarfile

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz, tarfile.open(fileobj=gz, mode="w"):
        pass
    return buf.getvalue()


@app.get("/health")
async def health() -> dict[str, Any]:
    try:
        code, out, err = await _run(DOCKER, "version", "--format", "{{.Server.Version}}", timeout=10)
    except (FileNotFoundError, TimeoutError) as exc:
        return {"status": "unavailable", "docker": False, "error": type(exc).__name__}
    if code != 0:
        return {"status": "unavailable", "docker": False, "error": err.decode(errors="replace")[:300]}
    images: dict[str, bool] = {}
    for key, profile in PROFILES.items():
        c, _, _ = await _run(DOCKER, "image", "inspect", profile["image"], timeout=10)
        images[key] = c == 0
    return {"status": "ok", "docker": True, "docker_version": out.decode().strip(), "runtime": RUNTIME or "runc",
            "profiles": {k: {"image": v["image"], "available": images[k]} for k, v in PROFILES.items()}}


@app.post("/v1/executions", response_model=ExecutionResult, dependencies=[Depends(require_token)])
async def create_execution(spec: Annotated[str, Form()], workspace: Annotated[UploadFile | None, File()] = None) -> ExecutionResult:
    try:
        parsed = ExecutionSpec.model_validate_json(spec)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors()) from exc
    archive = None
    if workspace is not None:
        archive = await workspace.read(MAX_ARCHIVE_BYTES + 1)
        if len(archive) > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, "Workspace archive is too large.")
    return await execute(parsed, archive)


async def _labeled_containers(project: str | None) -> list[dict[str, Any]]:
    args = [DOCKER, "ps", "-a", "--filter", "label=ownai.managed=true", "--format", "{{json .}}"]
    if project:
        if not _LABEL.match(project):
            raise HTTPException(400, "Invalid project label.")
        args[4:4] = ["--filter", f"label=ownai.project={project}"]
    code, out, err = await _run(*args, timeout=20)
    if code != 0:
        raise HTTPException(503, "Container runtime unavailable: " + err.decode(errors="replace")[:200])
    rows = []
    for line in out.decode().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append({k: data.get(k) for k in ("ID", "Names", "Image", "State", "Status", "CreatedAt", "Labels")})
    return rows


@app.get("/v1/containers", dependencies=[Depends(require_token)])
async def list_containers(project: str | None = None) -> dict[str, Any]:
    return {"containers": await _labeled_containers(project)}


@app.get("/v1/containers/{name}/logs", dependencies=[Depends(require_token)])
async def container_logs(name: str, project: str | None = None, tail: int = Query(200, ge=1, le=5000)) -> dict[str, Any]:
    if not _LABEL.match(name):
        raise HTTPException(400, "Invalid container name.")
    allowed = {c["Names"] for c in await _labeled_containers(project)}
    if name not in allowed:
        raise HTTPException(404, "Container not found or not managed by OwnAI.")
    code, out, err = await _run(DOCKER, "logs", "--tail", str(tail), name, timeout=20)
    text, truncated = _truncate(out + err)
    return {"name": name, "logs": text, "truncated": truncated}
