import io
import zipfile
from pathlib import Path

import pytest

from app.core.exceptions import ValidationFailed
from app.projects.analyzers.code_facts import detect_test_setup, find_endpoints, find_env_usage
from app.projects.analyzers.dependencies import analyze_dependencies
from app.projects.graph import resolve_imports
from app.projects.importer import extract_zip, sanitize_git_dir, validate_git_url
from app.projects.scanner import GitIgnore, detect_language, scan_project
from app.projects.symbols import extract_symbols
from app.rag.chunking import chunk_code, chunk_text, split_identifier
from app.rag.retrieval import build_tsquery, query_terms


def test_scan_sample_project(sample_project_dir: Path):
    scan = scan_project(sample_project_dir)
    paths = {f.path for f in scan.files}
    assert "taskboard/auth.py" in paths and "tests/test_auth.py" in paths
    env = next(f for f in scan.files if f.path == ".env")
    assert env.is_sensitive
    assert scan.languages["python"] >= 5


def test_gitignore_rules():
    gi = GitIgnore(["*.log", "build/", "/secret.txt", "!keep.log"])
    assert gi.ignored("app.log", False)
    assert not gi.ignored("keep.log", False)
    assert gi.ignored("build", True)
    assert gi.ignored("secret.txt", False)
    assert not gi.ignored("sub/secret.txt", False)


def test_language_detection():
    assert detect_language("a/b.tsx") == "tsx"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("docker-compose.yml") == "yaml"
    assert detect_language("x.unknownext") is None


def test_python_symbols_exact():
    code = "import os\n\nclass A:\n    def m(self):\n        return 1\n\n@deco\ndef f(x):\n    return x\n"
    result = extract_symbols("python", code)
    names = {(s.qualified_name, s.kind, s.start_line, s.end_line) for s in result.symbols}
    assert ("A", "class", 3, 5) in names
    assert ("A.m", "method", 4, 5) in names
    assert ("f", "function", 7, 9) in names  # decorator included
    assert "os" in result.imports


def test_endpoints_env_and_tests():
    code = '@app.get("/users/{id}")\nasync def get_user(id: int):\n    return os.getenv("DB_URL")\n'
    eps = find_endpoints("api.py", "python", code)
    assert eps[0].method == "GET" and eps[0].path == "/users/{id}" and eps[0].handler == "get_user"
    assert find_env_usage(code + "process.env.API_TOKEN") == {"DB_URL", "API_TOKEN"}
    setup = detect_test_setup(["tests/test_x.py"], set(), {})
    assert setup["framework"] == "pytest" and setup["command"][0] == "python"


def test_dependencies(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("fastapi==0.115.0\nhttpx>=0.27\n# comment\n")
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "^18"}, "devDependencies": {"vite": "5.0.0"}}')
    deps, errors = analyze_dependencies(tmp_path, ["requirements.txt", "package.json"])
    by_name = {d.name: d for d in deps}
    assert by_name["fastapi"].pinned and not by_name["httpx"].pinned
    assert by_name["vite"].dev and not errors


def test_import_resolution():
    files = {"app/core/config.py", "app/main.py", "app/__init__.py", "src/util/index.ts", "src/app.ts"}
    py = resolve_imports("app/main.py", "python", ["app.core.config", "os", ".core.config"], files)
    assert py[0].target == "app/core/config.py" and py[1].external and py[2].target == "app/core/config.py"
    ts = resolve_imports("src/app.ts", "typescript", ["./util", "react"], files)
    assert ts[0].target == "src/util/index.ts" and ts[1].external


def test_chunking_keeps_line_ranges():
    code = "\n".join(["import x", ""] + [f"def f{i}():\n    return {i}\n" for i in range(3)])
    symbols = extract_symbols("python", code).symbols
    chunks = chunk_code(code, symbols)
    lines = code.split("\n")
    for c in chunks:
        assert "\n".join(lines[c.start_line - 1:c.end_line]) == c.content
    assert {c.symbol for c in chunks if c.symbol} == {"f0", "f1", "f2"}
    text = "# Title\n\n" + "\n\n".join("para " * 80 for _ in range(10))
    tchunks = chunk_text(text, extract_symbols("markdown", text).symbols)
    assert len(tchunks) > 1 and tchunks[0].start_line == 1


def test_query_terms():
    assert split_identifier("getUserToken") == ["get", "user", "token"]
    terms = query_terms("Explain how authentication works in my project")
    assert "authentication" in terms and "auth" in terms and "how" not in terms
    assert "|" in build_tsquery(terms)


def _zip(entries: dict[str, str]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    path = Path(__import__("tempfile").mkstemp(suffix=".zip")[1])
    path.write_bytes(buf.getvalue())
    return path


def test_zip_extraction_and_zip_slip(tmp_path: Path):
    ok = _zip({"repo-main/src/a.py": "x", "repo-main/README.md": "r"})
    assert extract_zip(ok, tmp_path / "ok", max_bytes=10_000) == 2
    assert (tmp_path / "ok" / "src" / "a.py").exists()  # common top folder stripped
    evil = _zip({"../../evil.py": "x"})
    with pytest.raises(ValidationFailed):
        extract_zip(evil, tmp_path / "evil", max_bytes=10_000)
    bomb = _zip({"a.txt": "x" * 50_000})
    with pytest.raises(ValidationFailed):
        extract_zip(bomb, tmp_path / "bomb", max_bytes=1000)


def test_git_sanitization(tmp_path: Path):
    git = tmp_path / ".git"
    (git / "hooks").mkdir(parents=True)
    (git / "hooks" / "pre-commit").write_text("#!/bin/sh\nrm -rf ~")
    (git / "config").write_text('[core]\n\tfsmonitor = evil.sh\n\tbare = false\n[filter "lfs"]\n\tclean = evil\n'
                                '[remote "origin"]\n\turl = https://x/y.git\n')
    sanitize_git_dir(tmp_path)
    config = (git / "config").read_text()
    assert "fsmonitor" not in config and "filter" not in config and "origin" in config
    assert not (git / "hooks" / "pre-commit").exists()


def test_git_url_validation():
    assert validate_git_url("https://github.com/a/b.git")
    assert validate_git_url("git@github.com:a/b.git")
    for bad in ["file:///etc", "--upload-pack=evil", "ext::sh -c id"]:
        with pytest.raises(ValidationFailed):
            validate_git_url(bad)
