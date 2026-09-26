"""Client for the sandbox runner service + workspace archive builder."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.exceptions import ErrorCode, SandboxError
from app.core.logging import get_logger
from app.projects.scanner import DEFAULT_IGNORED_DIRS
from app.security.commands import validate_argv
from app.security.secrets import is_sensitive_file

log = get_logger(__name__)


@dataclass
class SandboxResult:
    execution_id: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    oom_killed: bool
    truncated: bool
    image: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def build_workspace_archive(root: Path | None, overlay: dict[str, str | None] | None = None,
                            *, max_bytes: int = 200 * 1024 * 1024) -> bytes:
    """tar.gz of the project (minus VCS dirs, dependency dirs and credential files) with ``overlay`` applied.

    ``overlay`` maps relative paths to new content (``None`` deletes the file) — used to test
    proposed changes before they are applied to the real project.
    """
    overlay = overlay or {}
    buf = io.BytesIO()
    total = 0
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=5) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        if root is not None:
            root = root.resolve()
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root).as_posix()
                parts = rel.split("/")
                if any(p in DEFAULT_IGNORED_DIRS for p in parts[:-1]) or parts[0] in DEFAULT_IGNORED_DIRS:
                    continue
                if path.is_symlink() or not path.is_file() or is_sensitive_file(rel) or rel in overlay:
                    continue
                size = path.stat().st_size
                total += size
                if total > max_bytes:
                    raise SandboxError("The project is too large to send to the sandbox.", code=ErrorCode.SANDBOX_FAILED)
                info = tarfile.TarInfo(rel)
                info.size = size
                info.mode = 0o644
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
        for rel, content in overlay.items():
            if content is None:
                continue
            data = content.encode("utf-8")
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class SandboxClient:
    def __init__(self, url: str | None, token: str, http: httpx.AsyncClient, *, default_timeout_s: int = 120) -> None:
        self.url = url.rstrip("/") if url else None
        self.token = token
        self.http = http
        self.default_timeout_s = default_timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _unavailable(self, detail: str) -> SandboxError:
        return SandboxError(
            "Code execution could not start because the sandbox service is unavailable.",
            code=ErrorCode.SANDBOX_UNAVAILABLE, detail=detail,
            hint="Start the sandbox runner (see docs/SANDBOX.md) and check OWNAI_SANDBOX_URL / OWNAI_SANDBOX_TOKEN.",
        )

    async def health(self) -> dict[str, Any]:
        if not self.url:
            return {"status": "not_configured"}
        try:
            response = await self.http.get(f"{self.url}/health", timeout=10)
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

    async def execute(self, *, profile: str, argv: list[str], workspace: Path | None = None,
                      overlay: dict[str, str | None] | None = None, timeout_s: int | None = None,
                      project_label: str = "none", workdir: str = "", env: dict[str, str] | None = None,
                      memory_mb: int | None = None) -> SandboxResult:
        if not self.url:
            raise self._unavailable("OWNAI_SANDBOX_URL is not configured")
        argv = validate_argv(argv, profile)  # defense in depth: the runner validates again
        archive = await asyncio.to_thread(build_workspace_archive, workspace, overlay)
        spec: dict[str, Any] = {
            "profile": profile, "argv": argv, "timeout_s": timeout_s or self.default_timeout_s,
            "project": project_label, "workdir": workdir, "env": env or {}, "execution_id": uuid.uuid4().hex,
        }
        if memory_mb:
            spec["memory_mb"] = memory_mb
        try:
            response = await self.http.post(
                f"{self.url}/v1/executions", headers=self._headers(),
                data={"spec": json.dumps(spec)},
                files={"workspace": ("workspace.tar.gz", archive, "application/gzip")},
                timeout=(timeout_s or self.default_timeout_s) + 60,
            )
        except httpx.HTTPError as exc:
            raise self._unavailable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code in (401, 403) and "token" in response.text.lower():
            raise SandboxError("The sandbox rejected the request (authentication).", code=ErrorCode.SANDBOX_UNAVAILABLE,
                               detail=response.text[:300], hint="OWNAI_SANDBOX_TOKEN must match SANDBOX_TOKEN on the runner.")
        if response.status_code == 403:
            raise SandboxError(response.json().get("detail", "Command not allowed."), code=ErrorCode.COMMAND_NOT_ALLOWED)
        if response.status_code >= 400:
            raise SandboxError(f"The sandbox rejected the request (HTTP {response.status_code}).",
                               code=ErrorCode.SANDBOX_FAILED, detail=response.text[:500])
        data = response.json()
        if not data.get("started", True):
            raise SandboxError("The sandbox container could not start.", code=ErrorCode.SANDBOX_FAILED,
                               detail=data.get("error"), hint="Check that the sandbox image is built (see docs/SANDBOX.md).")
        return SandboxResult(**{k: data[k] for k in SandboxResult.__dataclass_fields__})

    async def containers(self, project_label: str | None = None) -> list[dict[str, Any]]:
        if not self.url:
            raise self._unavailable("OWNAI_SANDBOX_URL is not configured")
        try:
            response = await self.http.get(f"{self.url}/v1/containers", headers=self._headers(),
                                           params={"project": project_label} if project_label else None, timeout=30)
        except httpx.HTTPError as exc:
            raise self._unavailable(str(exc)) from exc
        if response.status_code >= 400:
            raise SandboxError("Could not list containers.", detail=response.text[:300])
        return response.json().get("containers", [])

    async def logs(self, name: str, project_label: str | None, tail: int = 200) -> dict[str, Any]:
        if not self.url:
            raise self._unavailable("OWNAI_SANDBOX_URL is not configured")
        try:
            response = await self.http.get(f"{self.url}/v1/containers/{name}/logs", headers=self._headers(),
                                           params={"tail": tail, **({"project": project_label} if project_label else {})},
                                           timeout=30)
        except httpx.HTTPError as exc:
            raise self._unavailable(str(exc)) from exc
        if response.status_code == 404:
            raise SandboxError("Container not found or not managed by OwnAI.", code=ErrorCode.NOT_FOUND)
        if response.status_code >= 400:
            raise SandboxError("Could not read container logs.", detail=response.text[:300])
        return response.json()
