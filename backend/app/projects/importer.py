"""Project import: zip upload, git clone, local path (copy or link).

Hardening:
* zip: path traversal (zip-slip), absolute paths, symlinks, file-count and size bombs;
* git: https/ssh/file URLs only, shallow clone, no submodules, hooks disabled, timeout;
* any imported ``.git`` directory is sanitised (hooks removed, executable config keys
  stripped) because git may run commands from repository config.
"""

from __future__ import annotations

import asyncio
import configparser
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from app.core.exceptions import ErrorCode, ValidationFailed
from app.core.logging import get_logger
from app.security.paths import safe_join_under

log = get_logger(__name__)

MAX_ZIP_ENTRIES = 50_000
_ALLOWED_GIT_SCHEMES = {"https", "http", "ssh", "git"}
# git config keys that can execute programs
_UNSAFE_GIT_KEYS = re.compile(
    r"^(core\.(fsmonitor|hookspath|sshcommand|editor|pager|askpass|gitproxy)|"
    r"(diff|merge|filter)\..*|credential\..*|.*\.(command|cmd|textconv|driver|clean|smudge|process)|"
    r"uploadpack\..*|receive\..*|sequence\.editor|gpg\..*|include\..*|includeif\..*)$",
    re.IGNORECASE,
)


def strip_url_credentials(url: str) -> str:
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def validate_git_url(url: str) -> str:
    url = url.strip()
    if url.startswith("-"):
        raise ValidationFailed("Invalid repository URL.", code=ErrorCode.IMPORT_FAILED)
    if re.match(r"^[\w.\-]+@[\w.\-]+:[\w./\-]+$", url):  # scp-like ssh syntax
        return url
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_GIT_SCHEMES or not parts.netloc:
        raise ValidationFailed("Only https://, ssh:// and git@host:repo URLs can be imported.", code=ErrorCode.IMPORT_FAILED)
    return url


def sanitize_git_dir(project_root: Path) -> None:
    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        if git_dir.exists():  # .git file (worktree/submodule pointer) — not supported for imports
            git_dir.unlink()
        return
    hooks = git_dir / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks, ignore_errors=True)
    hooks.mkdir(exist_ok=True)
    config_path = git_dir / "config"
    if not config_path.exists():
        return
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error:
        config_path.write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n", encoding="utf-8")
        return
    for section in list(parser.sections()):
        base = section.split(" ", 1)[0].strip('"').lower()
        if base in {"filter", "diff", "merge", "credential", "include", "includeif", "gpg", "uploadpack", "receive"}:
            parser.remove_section(section)
            continue
        for key in list(parser.options(section)):
            if _UNSAFE_GIT_KEYS.match(f"{base}.{key}"):
                parser.remove_option(section, key)
    with config_path.open("w", encoding="utf-8") as fh:
        parser.write(fh)
    for attr_file in (git_dir / "info" / "attributes",):
        if attr_file.exists():
            attr_file.unlink()


def extract_zip(archive: Path, destination: Path, *, max_bytes: int) -> int:
    """Safely extract a zip archive. Returns the number of files extracted."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise ValidationFailed("The uploaded file is not a valid zip archive.", code=ErrorCode.IMPORT_FAILED) from exc
    with zf:
        members = zf.infolist()
        if len(members) > MAX_ZIP_ENTRIES:
            raise ValidationFailed(f"The archive has too many entries (>{MAX_ZIP_ENTRIES}).", code=ErrorCode.IMPORT_FAILED)
        total = sum(m.file_size for m in members)
        if total > max_bytes:
            raise ValidationFailed(f"The archive expands to {total // (1024 * 1024)} MB, over the limit.",
                                   code=ErrorCode.IMPORT_FAILED)
        # strip a single common top-level folder (typical for GitHub zip downloads)
        names = [m.filename for m in members if m.filename and not m.filename.startswith("__MACOSX/")]
        tops = {PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts}
        strip = tops.pop() if len(tops) == 1 and all("/" in n or n.endswith("/") for n in names) else None
        count = 0
        for member in members:
            name = member.filename.replace("\\", "/")
            if not name or name.startswith("__MACOSX/"):
                continue
            mode = (member.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                continue  # never materialise symlinks from archives
            parts = PurePosixPath(name).parts
            if strip and parts and parts[0] == strip:
                parts = parts[1:]
            if not parts:
                continue
            if name.startswith("/") or any(p in ("..", "") for p in parts) or re.match(r"^[A-Za-z]:", parts[0]):
                raise ValidationFailed(f"Unsafe path in archive: {name}", code=ErrorCode.IMPORT_FAILED)
            target = (root / Path(*parts)).resolve()
            if root not in target.parents and target != root:
                raise ValidationFailed(f"Unsafe path in archive: {name}", code=ErrorCode.IMPORT_FAILED)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            count += 1
    return count


async def git_clone(url: str, destination: Path, *, branch: str | None = None, timeout_s: int = 300) -> None:
    url = validate_git_url(url)
    args = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never",
            "clone", "--depth", "50", "--no-recurse-submodules", "--single-branch"]
    if branch:
        if branch.startswith("-") or not re.fullmatch(r"[\w./\-]+", branch):
            raise ValidationFailed("Invalid branch name.", code=ErrorCode.IMPORT_FAILED)
        args += ["--branch", branch]
    args += ["--", url, str(destination)]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, env=env)
    except FileNotFoundError as exc:
        raise ValidationFailed("Git is not installed on the OwnAI server; import a .zip instead.",
                               code=ErrorCode.IMPORT_FAILED) from exc
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        raise ValidationFailed("Cloning the repository timed out.", code=ErrorCode.IMPORT_FAILED) from exc
    if proc.returncode != 0:
        message = stderr.decode(errors="replace").strip().splitlines()[-1:] or ["unknown error"]
        raise ValidationFailed(f"git clone failed: {strip_url_credentials(message[0])}", code=ErrorCode.IMPORT_FAILED,
                               detail=strip_url_credentials(stderr.decode(errors='replace')))
    sanitize_git_dir(destination)


def copy_local(source: str, destination: Path, allowed_roots: list[Path]) -> Path:
    if not allowed_roots:
        raise ValidationFailed("Local path imports are disabled. Set OWNAI_LOCAL_IMPORT_ROOTS to enable them.",
                               code=ErrorCode.IMPORT_FAILED)
    src = safe_join_under(allowed_roots, source)
    if not src.is_dir():
        raise ValidationFailed(f"'{source}' is not a directory.", code=ErrorCode.IMPORT_FAILED)
    ignore = shutil.ignore_patterns("node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache",
                                    ".pytest_cache", ".ruff_cache", ".tox")
    shutil.copytree(src, destination, symlinks=True, ignore=ignore, dirs_exist_ok=False)
    # drop copied symlinks that point outside the project
    for path in destination.rglob("*"):
        if path.is_symlink():
            try:
                target = path.resolve()
            except OSError:
                path.unlink()
                continue
            if destination.resolve() not in target.parents:
                path.unlink()
    sanitize_git_dir(destination)
    return src


def link_local(source: str, allowed_roots: list[Path]) -> Path:
    if not allowed_roots:
        raise ValidationFailed("Local path imports are disabled. Set OWNAI_LOCAL_IMPORT_ROOTS to enable them.",
                               code=ErrorCode.IMPORT_FAILED)
    src = safe_join_under(allowed_roots, source)
    if not src.is_dir():
        raise ValidationFailed(f"'{source}' is not a directory.", code=ErrorCode.IMPORT_FAILED)
    return src
