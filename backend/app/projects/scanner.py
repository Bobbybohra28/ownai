"""Project file scanner: walks a project, applies ignore rules, detects languages and sensitive files."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.security.secrets import is_sensitive_file

LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "jsx", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift", ".scala": "scala",
    ".sql": "sql", ".md": "markdown", ".mdx": "markdown", ".rst": "rst", ".txt": "text",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".xml": "xml", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".sh": "shell", ".bash": "shell", ".ps1": "powershell", ".vue": "vue", ".svelte": "svelte",
    ".proto": "protobuf", ".graphql": "graphql", ".gql": "graphql", ".tf": "terraform",
    ".env.example": "dotenv",
}
LANGUAGE_BY_FILENAME: dict[str, str] = {
    "dockerfile": "dockerfile", "makefile": "makefile", "jenkinsfile": "groovy", "procfile": "text",
    "requirements.txt": "requirements", "go.mod": "gomod", "cargo.toml": "toml", "pipfile": "toml",
    ".gitignore": "text", ".dockerignore": "text", ".env.example": "dotenv", ".env.sample": "dotenv",
}
CODE_LANGUAGES = {
    "python", "javascript", "jsx", "typescript", "tsx", "java", "kotlin", "go", "rust", "c", "cpp", "csharp",
    "ruby", "php", "swift", "scala", "shell", "powershell", "vue", "svelte", "sql",
}

DEFAULT_IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", "coverage",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".idea", ".vscode", ".next", ".nuxt", ".svelte-kit",
    "target", ".gradle", ".terraform", "site-packages", ".cache", ".parcel-cache", "htmlcov", ".eggs", "bower_components",
    ".turbo", ".output", ".serverless", ".aws-sam", ".ipynb_checkpoints",
}
DEFAULT_IGNORED_FILES = (
    "*.pyc", "*.pyo", "*.so", "*.dll", "*.dylib", "*.exe", "*.o", "*.a", "*.class", "*.jar", "*.war",
    "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp", "*.bmp", "*.svgz", "*.pdf", "*.zip", "*.tar", "*.gz",
    "*.tgz", "*.bz2", "*.7z", "*.rar", "*.mp3", "*.mp4", "*.mov", "*.avi", "*.woff", "*.woff2", "*.ttf", "*.eot",
    "*.sqlite", "*.sqlite3", "*.db", "*.parquet", "*.pkl", "*.pt", "*.bin", "*.onnx", "*.gguf", "*.safetensors",
    ".DS_Store", "Thumbs.db",
)
_TEST_HINTS = ("test_", "_test.", ".test.", ".spec.", "tests/", "test/", "__tests__/", "spec/")


def detect_language(path: str) -> str | None:
    posix = PurePosixPath(path)
    name = posix.name.lower()
    if name in LANGUAGE_BY_FILENAME:
        return LANGUAGE_BY_FILENAME[name]
    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return "dockerfile"
    if name.startswith("docker-compose") or name.startswith("compose."):
        return "yaml"
    return LANGUAGE_BY_EXTENSION.get(posix.suffix.lower())


def is_test_path(path: str) -> bool:
    lowered = path.lower()
    return any(hint in lowered for hint in _TEST_HINTS)


class GitIgnore:
    """A pragmatic subset of .gitignore semantics (globs, dir patterns, anchoring, negation)."""

    def __init__(self, lines: list[str]) -> None:
        self.rules: list[tuple[str, bool, bool, bool]] = []  # (pattern, negate, dir_only, anchored)
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            anchored = line.startswith("/") or "/" in line
            line = line.lstrip("/")
            if line:
                self.rules.append((line, negate, dir_only, anchored))

    @classmethod
    def from_root(cls, root: Path) -> GitIgnore:
        path = root / ".gitignore"
        try:
            return cls(path.read_text(encoding="utf-8", errors="ignore").splitlines()) if path.is_file() else cls([])
        except OSError:
            return cls([])

    def ignored(self, rel_path: str, is_dir: bool) -> bool:
        result = False
        name = PurePosixPath(rel_path).name
        for pattern, negate, dir_only, anchored in self.rules:
            if dir_only and not is_dir:
                continue
            target = rel_path if anchored else name
            if fnmatch.fnmatch(target, pattern) or (anchored and fnmatch.fnmatch(rel_path, pattern + "/*")):
                result = not negate
        return result


@dataclass
class ScannedFile:
    path: str  # project-relative POSIX path
    size: int
    sha256: str
    language: str | None
    line_count: int
    is_sensitive: bool
    is_test: bool
    is_binary: bool = False


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    skipped_ignored: int = 0
    skipped_binary: int = 0
    skipped_large: int = 0
    truncated: bool = False

    @property
    def languages(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.files:
            if f.language and not f.is_sensitive:
                counts[f.language] = counts.get(f.language, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _is_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    text_chars = bytes(range(32, 127)) + b"\n\r\t\f\b"
    nontext = sum(1 for b in sample if b not in text_chars and b < 128)
    return nontext / len(sample) > 0.3


def scan_project(root: Path, *, max_files: int = 20_000, max_file_kb: int = 512,
                 extra_ignores: list[str] | None = None) -> ScanResult:
    root = root.resolve()
    gitignore = GitIgnore.from_root(root)
    extra = GitIgnore(extra_ignores or [])
    result = ScanResult()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        kept: list[str] = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            full = Path(dirpath) / d
            if d in DEFAULT_IGNORED_DIRS or d.endswith(".egg-info") or full.is_symlink() or \
                    gitignore.ignored(rel, True) or extra.ignored(rel, True):
                result.skipped_ignored += 1
                continue
            kept.append(d)
            result.directories.append(rel)
        dirnames[:] = kept
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = Path(dirpath) / name
            if full.is_symlink() or any(fnmatch.fnmatch(name, pat) for pat in DEFAULT_IGNORED_FILES) or \
                    gitignore.ignored(rel, False) or extra.ignored(rel, False):
                result.skipped_ignored += 1
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            sensitive = is_sensitive_file(rel)
            if size > max_file_kb * 1024 and not sensitive:
                result.skipped_large += 1
                continue
            try:
                data = full.read_bytes()
            except OSError:
                continue
            if _is_binary(data[:8192]):
                result.skipped_binary += 1
                continue
            if len(result.files) >= max_files:
                result.truncated = True
                return result
            result.files.append(ScannedFile(
                path=rel, size=size, sha256=hashlib.sha256(data).hexdigest(), language=detect_language(rel),
                line_count=data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
                is_sensitive=sensitive, is_test=is_test_path(rel),
            ))
    return result


def render_tree(paths: list[str], *, max_depth: int = 3, max_entries: int = 400) -> str:
    """Render a compact directory tree for prompts and the UI."""
    tree: dict[str, dict] = {}
    for p in sorted(paths):
        node = tree
        for part in PurePosixPath(p).parts[:max_depth]:
            node = node.setdefault(part, {})
    lines: list[str] = []

    def walk(node: dict, prefix: str) -> None:
        for name in sorted(node, key=lambda n: (not node[n], n)):
            if len(lines) >= max_entries:
                return
            lines.append(f"{prefix}{name}{'/' if node[name] else ''}")
            walk(node[name], prefix + "  ")

    walk(tree, "")
    if len(lines) >= max_entries:
        lines.append("…")
    return "\n".join(lines)
