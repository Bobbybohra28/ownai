"""Path jail: every file access by tools is resolved and confined to a project root."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from app.core.exceptions import ErrorCode, NotFoundError, PermissionDenied
from app.security.secrets import is_sensitive_file

# Directories that tools may never read or write inside a project.
DENIED_DIR_PARTS = {".git", ".hg", ".svn"}


class PathJail:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def normalize(self, relative: str) -> str:
        """Return a clean project-relative POSIX path or raise."""
        if relative is None or "\x00" in relative:
            raise PermissionDenied("Invalid path.", code=ErrorCode.PATH_NOT_ALLOWED)
        cleaned = relative.replace("\\", "/").strip()
        if cleaned in ("", "."):
            return ""
        if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
            # absolute paths are only accepted if they point inside the root
            candidate = Path(cleaned)
            try:
                cleaned = candidate.resolve().relative_to(self.root).as_posix()
            except ValueError as exc:
                raise PermissionDenied(f"Path '{relative}' is outside the project.",
                                       code=ErrorCode.PATH_NOT_ALLOWED) from exc
        parts = PurePosixPath(cleaned).parts
        if any(p == ".." for p in parts):
            raise PermissionDenied(f"Path '{relative}' escapes the project directory.", code=ErrorCode.PATH_NOT_ALLOWED)
        return PurePosixPath(*parts).as_posix() if parts else ""

    def resolve(self, relative: str, *, allow_sensitive: bool = False, must_exist: bool = False) -> Path:
        rel = self.normalize(relative)
        target = (self.root / rel).resolve() if rel else self.root
        # symlinks must not escape the root
        if target != self.root and self.root not in target.parents:
            raise PermissionDenied(f"Path '{relative}' resolves outside the project (symlink escape).",
                                   code=ErrorCode.PATH_NOT_ALLOWED)
        rel_parts = PurePosixPath(rel).parts
        if any(p in DENIED_DIR_PARTS for p in rel_parts):
            raise PermissionDenied(f"Access to '{relative}' is not allowed (VCS internals).", code=ErrorCode.PATH_NOT_ALLOWED)
        if not allow_sensitive and rel and is_sensitive_file(rel):
            raise PermissionDenied(
                f"'{relative}' may contain credentials; its contents are never sent to AI models.",
                code=ErrorCode.PATH_NOT_ALLOWED,
            )
        if must_exist and not target.exists():
            raise NotFoundError(f"File '{rel}' does not exist in the project.")
        return target

    def relative(self, absolute: Path) -> str:
        return absolute.resolve().relative_to(self.root).as_posix()


def is_within(root: Path, candidate: Path) -> bool:
    root = root.resolve()
    candidate = candidate.resolve()
    return candidate == root or root in candidate.parents


def safe_join_under(roots: list[Path], path: str) -> Path:
    """Validate that ``path`` (absolute) is inside one of ``roots`` (used for local imports)."""
    candidate = Path(os.path.expanduser(path)).resolve()
    for root in roots:
        if is_within(root, candidate):
            return candidate
    raise PermissionDenied(
        "Local imports are only allowed from configured directories (OWNAI_LOCAL_IMPORT_ROOTS).",
        code=ErrorCode.PATH_NOT_ALLOWED,
    )
