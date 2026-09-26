"""Static facts extracted from source text: API endpoints, env-var usage, database
connections, framework markers and test setup. All regex-based and read-only."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from app.security.secrets import mask_secrets


@dataclass
class Endpoint:
    method: str
    path: str
    file: str
    line: int
    framework: str
    handler: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


_PY_ROUTE = re.compile(
    r"""@(?P<obj>\w+)\.(?P<method>get|post|put|patch|delete|options|head|websocket|api_route|route)\(\s*[rf]?["'](?P<path>[^"']*)["']""",
    re.IGNORECASE,
)
_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")
_FLASK_METHODS = re.compile(r"methods\s*=\s*\[([^\]]+)\]")
_JS_ROUTE = re.compile(
    r"""\b(?P<obj>app|router|server|api|fastify)\.(?P<method>get|post|put|patch|delete|all|use|options)\(\s*["'`](?P<path>/[^"'`]*)["'`]"""
)
_SPRING_ROUTE = re.compile(
    r"""@(?P<method>Get|Post|Put|Patch|Delete|Request)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?\{?\s*"(?P<path>[^"]*)\""""
)
_GO_ROUTE = re.compile(
    r"""\.(?P<method>GET|POST|PUT|PATCH|DELETE|HandleFunc|Handle|Get|Post|Put|Delete|Patch)\(\s*"(?P<path>/[^"]*)\""""
)

_ENV_PATTERNS = [
    re.compile(r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z_][A-Z0-9_]*)["']"""),
    re.compile(r"""os\.environ\[\s*["']([A-Z_][A-Z0-9_]*)["']\s*\]"""),
    re.compile(r"""process\.env\.([A-Z_][A-Z0-9_]*)"""),
    re.compile(r"""process\.env\[\s*["']([A-Z_][A-Z0-9_]*)["']\s*\]"""),
    re.compile(r"""import\.meta\.env\.([A-Z_][A-Z0-9_]*)"""),
    re.compile(r"""System\.getenv\(\s*"([A-Z_][A-Z0-9_]*)"\s*\)"""),
    re.compile(r"""os\.Getenv\(\s*"([A-Z_][A-Z0-9_]*)"\s*\)"""),
    re.compile(r"""env::var\(\s*"([A-Z_][A-Z0-9_]*)"\s*\)"""),
    re.compile(r"""\$\{([A-Z_][A-Z0-9_]*)(?::-[^}]*)?\}"""),
]

_DB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sqlalchemy", re.compile(r"\bcreate_(?:async_)?engine\(")),
    ("postgresql", re.compile(r"\bpostgres(?:ql)?(?:\+\w+)?://|\bpsycopg2?\b|\basyncpg\b|\bpg\.Pool\b|\bnew\s+Pool\(")),
    ("mysql", re.compile(r"\bmysql(?:\+\w+)?://|\bpymysql\b|\bmysql2\b")),
    ("sqlite", re.compile(r"\bsqlite(?:\+\w+)?:///|\bsqlite3\.connect\(")),
    ("mongodb", re.compile(r"\bmongodb(?:\+srv)?://|\bMongoClient\(|\bmongoose\.connect\(")),
    ("redis", re.compile(r"\bredis://|\bRedis\(|\bcreateClient\(")),
    ("prisma", re.compile(r"\bPrismaClient\(")),
    ("typeorm", re.compile(r"\bDataSource\(|\bcreateConnection\(")),
    ("jdbc", re.compile(r"\bjdbc:\w+:")),
    ("gorm", re.compile(r"\bgorm\.Open\(")),
]

FRAMEWORK_MARKERS: dict[str, tuple[str, ...]] = {
    "FastAPI": ("fastapi",),
    "Django": ("django",),
    "Flask": ("flask",),
    "SQLAlchemy": ("sqlalchemy",),
    "Pydantic": ("pydantic",),
    "Celery": ("celery",),
    "pytest": ("pytest",),
    "React": ("react",),
    "Next.js": ("next",),
    "Vue": ("vue",),
    "Angular": ("@angular/core",),
    "Svelte": ("svelte",),
    "Express": ("express",),
    "NestJS": ("@nestjs/core",),
    "Vite": ("vite",),
    "Jest": ("jest",),
    "Vitest": ("vitest",),
    "Prisma": ("prisma", "@prisma/client"),
    "Spring Boot": ("org.springframework.boot:spring-boot-starter-web", "org.springframework.boot:spring-boot-starter"),
    "Gin": ("github.com/gin-gonic/gin",),
    "Echo": ("github.com/labstack/echo/v4",),
    "Actix": ("actix-web",),
    "Axum": ("axum",),
    "Tokio": ("tokio",),
    "TensorFlow": ("tensorflow",),
    "PyTorch": ("torch",),
    "scikit-learn": ("scikit-learn",),
    "pandas": ("pandas",),
}


def find_endpoints(path: str, language: str | None, text: str) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    lines = text.splitlines()
    if language == "python":
        for idx, line in enumerate(lines):
            for m in _PY_ROUTE.finditer(line):
                method = m.group("method").upper()
                framework = "FastAPI" if method not in {"ROUTE"} else "Flask"
                if method == "ROUTE":
                    methods = _FLASK_METHODS.search(line)
                    method = methods.group(1).replace("'", "").replace('"', "").replace(" ", "") if methods else "GET"
                handler = None
                for look in lines[idx + 1: idx + 6]:
                    d = _PY_DEF.match(look)
                    if d:
                        handler = d.group(1)
                        break
                endpoints.append(Endpoint(method, m.group("path") or "/", path, idx + 1, framework, handler))
    elif language in {"javascript", "typescript", "jsx", "tsx"}:
        for idx, line in enumerate(lines):
            for m in _JS_ROUTE.finditer(line):
                if m.group("method") == "use":
                    continue
                endpoints.append(Endpoint(m.group("method").upper(), m.group("path"), path, idx + 1, "Express-style"))
    elif language in {"java", "kotlin"}:
        for idx, line in enumerate(lines):
            for m in _SPRING_ROUTE.finditer(line):
                method = m.group("method").upper()
                endpoints.append(Endpoint("ANY" if method == "REQUEST" else method, m.group("path"), path, idx + 1, "Spring"))
    elif language == "go":
        for idx, line in enumerate(lines):
            for m in _GO_ROUTE.finditer(line):
                method = m.group("method").upper()
                endpoints.append(Endpoint("ANY" if method.startswith("HANDLE") else method, m.group("path"),
                                          path, idx + 1, "Go HTTP"))
    return endpoints


def find_env_usage(text: str) -> set[str]:
    names: set[str] = set()
    for pattern in _ENV_PATTERNS:
        names.update(pattern.findall(text))
    return {n for n in names if len(n) > 1}


def find_db_usage(path: str, text: str) -> list[dict]:
    found: list[dict] = []
    for kind, pattern in _DB_PATTERNS:
        m = pattern.search(text)
        if m:
            line_no = text.count("\n", 0, m.start()) + 1
            line = text.splitlines()[line_no - 1] if text else ""
            found.append({"kind": kind, "file": path, "line": line_no, "snippet": mask_secrets(line.strip())[:160]})
    return found


def detect_frameworks(dependency_names: set[str], files: list[str]) -> list[str]:
    lowered = {d.lower() for d in dependency_names}
    frameworks = [name for name, markers in FRAMEWORK_MARKERS.items() if any(m.lower() in lowered for m in markers)]
    names = {f.rsplit("/", 1)[-1] for f in files}
    if "manage.py" in names and "Django" not in frameworks:
        frameworks.append("Django")
    if any(n.lower().startswith("dockerfile") for n in names):
        frameworks.append("Docker")
    if any(n.startswith(("docker-compose", "compose.")) for n in names):
        frameworks.append("Docker Compose")
    if any(f.startswith(".github/workflows/") for f in files):
        frameworks.append("GitHub Actions")
    if any(f.endswith(".tf") for f in files):
        frameworks.append("Terraform")
    if any("/templates/" in f and f.endswith((".yaml", ".yml")) for f in files) and "Chart.yaml" in names:
        frameworks.append("Helm")
    if any(f.startswith(("k8s/", "kubernetes/", "manifests/")) for f in files):
        frameworks.append("Kubernetes")
    return frameworks


def detect_test_setup(files: list[str], dependency_names: set[str], package_scripts: dict[str, str]) -> dict:
    """Infer how tests are run. Returns {framework, command (argv), test_files}."""
    test_files = [f for f in files if re_test_file.search(f)]
    lowered = {d.lower() for d in dependency_names}
    py_tests = [f for f in test_files if f.endswith(".py")]
    if py_tests or "pytest" in lowered:
        return {"framework": "pytest", "profile": "python", "command": ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
                "test_files": test_files[:200]}
    if "test" in package_scripts:
        return {"framework": "npm", "profile": "node", "command": ["npm", "test", "--silent"], "test_files": test_files[:200]}
    if any(f.endswith("_test.go") for f in files):
        return {"framework": "go test", "profile": "go", "command": ["go", "test", "./..."], "test_files": test_files[:200]}
    if "Cargo.toml" in {f.rsplit("/", 1)[-1] for f in files}:
        return {"framework": "cargo test", "profile": "rust", "command": ["cargo", "test", "--offline"], "test_files": test_files[:200]}
    return {"framework": None, "profile": None, "command": None, "test_files": test_files[:200]}


re_test_file = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.(py|go)|[^/]+\.(test|spec)\.[jt]sx?|tests?/[^/]+\.[a-z]+|__tests__/)")
