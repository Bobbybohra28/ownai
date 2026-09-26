"""Dependency manifest parsing (no network access, no package installs)."""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET  # noqa: S405 - parsing local pom.xml, entities disabled below
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Dependency:
    name: str
    version: str | None
    ecosystem: str  # pypi|npm|go|cargo|maven
    manifest: str
    dev: bool = False

    @property
    def pinned(self) -> bool:
        return bool(self.version) and bool(re.match(r"^=?=?\s*v?\d+(\.\d+)*([-+.][\w.]+)?$", self.version or ""))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["pinned"] = self.pinned
        return data


_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*(?:(==|>=|<=|~=|!=|>|<)\s*([^\s;#,]+))?")


def _requirements(path: Path, rel: str) -> list[Dependency]:
    deps: list[Dependency] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-", "git+", "http://", "https://")):
            continue
        m = _REQ_LINE.match(line)
        if m:
            version = f"{m.group(2)}{m.group(3)}" if m.group(2) else None
            if version and version.startswith("=="):
                version = version[2:]
            deps.append(Dependency(m.group(1).split("[")[0].lower(), version, "pypi", rel))
    return deps


def _split_pep508(spec: str) -> tuple[str, str | None]:
    m = re.match(r"^\s*([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(.*)$", spec)
    if not m:
        return spec.strip(), None
    version = m.group(2).split(";")[0].strip() or None
    if version and version.startswith("=="):
        version = version[2:]
    return m.group(1).lower(), version


def _pyproject(path: Path, rel: str) -> list[Dependency]:
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    deps: list[Dependency] = []
    project = data.get("project") or {}
    for spec in project.get("dependencies") or []:
        name, version = _split_pep508(spec)
        deps.append(Dependency(name, version, "pypi", rel))
    for group in (project.get("optional-dependencies") or {}).values():
        for spec in group:
            name, version = _split_pep508(spec)
            deps.append(Dependency(name, version, "pypi", rel, dev=True))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    for section, dev in (("dependencies", False), ("dev-dependencies", True)):
        for name, spec in (poetry.get(section) or {}).items():
            if name.lower() == "python":
                continue
            version = spec if isinstance(spec, str) else (spec or {}).get("version")
            deps.append(Dependency(name.lower(), version, "pypi", rel, dev=dev))
    return deps


def _package_json(path: Path, rel: str) -> list[Dependency]:
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}")
    deps: list[Dependency] = []
    for section, dev in (("dependencies", False), ("devDependencies", True), ("peerDependencies", False)):
        for name, version in (data.get(section) or {}).items():
            deps.append(Dependency(name, str(version), "npm", rel, dev=dev))
    return deps


def _go_mod(path: Path, rel: str) -> list[Dependency]:
    deps: list[Dependency] = []
    in_block = False
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("require ("):
            in_block = True
            continue
        if in_block and stripped == ")":
            in_block = False
            continue
        m = re.match(r"^(?:require\s+)?([\w.\-/]+\.[\w.\-/]+)\s+(v[\w.\-+]+)", stripped)
        if m and (in_block or stripped.startswith("require")):
            deps.append(Dependency(m.group(1), m.group(2), "go", rel, dev="// indirect" in stripped))
    return deps


def _cargo(path: Path, rel: str) -> list[Dependency]:
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    deps: list[Dependency] = []
    for section, dev in (("dependencies", False), ("dev-dependencies", True)):
        for name, spec in (data.get(section) or {}).items():
            version = spec if isinstance(spec, str) else (spec or {}).get("version")
            deps.append(Dependency(name, version, "cargo", rel, dev=dev))
    return deps


def _pom(path: Path, rel: str) -> list[Dependency]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if "<!ENTITY" in text:  # refuse entity expansion (billion laughs / XXE)
        return []
    root = ET.fromstring(text)  # noqa: S314 - entities rejected above
    ns = {"m": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    prefix = "m:" if ns else ""
    deps: list[Dependency] = []
    for dep in root.iter(f"{{{ns['m']}}}dependency" if ns else "dependency"):
        group = dep.findtext(f"{prefix}groupId", default="", namespaces=ns)
        artifact = dep.findtext(f"{prefix}artifactId", default="", namespaces=ns)
        version = dep.findtext(f"{prefix}version", default=None, namespaces=ns)
        scope = dep.findtext(f"{prefix}scope", default="", namespaces=ns)
        if artifact:
            deps.append(Dependency(f"{group}:{artifact}", version, "maven", rel, dev=scope == "test"))
    return deps


MANIFESTS = {
    "requirements.txt": _requirements,
    "requirements-dev.txt": _requirements,
    "pyproject.toml": _pyproject,
    "package.json": _package_json,
    "go.mod": _go_mod,
    "Cargo.toml": _cargo,
    "pom.xml": _pom,
}


def analyze_dependencies(root: Path, files: list[str]) -> tuple[list[Dependency], list[str]]:
    deps: list[Dependency] = []
    errors: list[str] = []
    for rel in files:
        name = rel.rsplit("/", 1)[-1]
        parser = MANIFESTS.get(name)
        if parser is None and re.fullmatch(r"requirements[\w\-]*\.txt", name):
            parser = _requirements
        if parser is None:
            continue
        try:
            deps.extend(parser(root / rel, rel))
        except (ValueError, OSError, tomllib.TOMLDecodeError, ET.ParseError) as exc:
            errors.append(f"{rel}: {type(exc).__name__}: {exc}")
    return deps, errors
