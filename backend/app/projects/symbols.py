"""Symbol and import extraction.

Python uses the standard-library ``ast`` module (exact). Other languages use
brace-aware regular-expression extractors that recover declarations and their
line ranges reliably for typical code; they are deliberately isolated behind
``extract_symbols`` so a tree-sitter backend can replace them without touching
callers.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field


@dataclass
class SymbolInfo:
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str = ""
    parent: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass
class ExtractionResult:
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: str | None = None


# ---------------------------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------------------------

def _py_signature(node: ast.AST, source_lines: list[str]) -> str:
    line = source_lines[node.lineno - 1].strip() if 0 < node.lineno <= len(source_lines) else ""  # type: ignore[attr-defined]
    return line[:300]


def _extract_python(text: str) -> ExtractionResult:
    result = ExtractionResult()
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        result.parse_error = f"SyntaxError at line {exc.lineno}: {exc.msg}"
        return result
    lines = text.splitlines()

    def visit(body: list[ast.stmt], parent: str | None) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                result.symbols.append(SymbolInfo(node.name, "class", node.lineno, node.end_lineno or node.lineno,
                                                 _py_signature(node, lines), parent))
                visit(node.body, f"{parent}.{node.name}" if parent else node.name)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                kind = "method" if parent and parent[:1].isupper() else "function"
                result.symbols.append(SymbolInfo(node.name, kind, start, node.end_lineno or node.lineno,
                                                 _py_signature(node, lines), parent))
                visit([n for n in node.body if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)],
                      f"{parent}.{node.name}" if parent else node.name)
            elif isinstance(node, ast.Assign) and parent is None:
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        result.symbols.append(SymbolInfo(target.id, "constant", node.lineno,
                                                         node.end_lineno or node.lineno, _py_signature(node, lines)))

    visit(tree.body, None)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            result.imports.append(module)
    return result


# ---------------------------------------------------------------------------------------------
# Brace languages
# ---------------------------------------------------------------------------------------------

_CONTROL = {"if", "for", "while", "switch", "catch", "return", "function", "else", "do", "try", "with", "new",
            "typeof", "await", "sizeof", "elif", "match", "loop", "unsafe"}


def _block_end(lines: list[str], start_idx: int) -> int:
    """Return the 0-based index of the line closing the block that opens at/after ``start_idx``."""
    depth = 0
    opened = False
    in_block_comment = False
    for idx in range(start_idx, min(len(lines), start_idx + 5000)):
        line = lines[idx]
        i = 0
        quote: str | None = None
        while i < len(line):
            ch = line[i]
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    i += 1
            elif quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch == "/" and nxt == "/":
                break
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                i += 1
            elif ch in "\"'`":
                quote = ch
            elif ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return idx
            elif ch == ";" and not opened and idx == start_idx:
                return idx  # declaration without body
            i += 1
        if not opened and idx > start_idx + 3:
            return start_idx  # no block found near the declaration
    return len(lines) - 1


_JS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^=]*>)?\s*=")),
    ("enum", re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[<(]")),
    ("function", re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*(?:async\s+)?"
        r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=>")),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b")),
    ("component", re.compile(r"^\s*(?:export\s+)?(?:const|let)\s+([A-Z][\w$]*)\s*(?::\s*[\w.<>, ]+)?=\s*(?:React\.)?(?:memo|forwardRef)\(")),
]
_JS_METHOD = re.compile(r"^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|get\s+|set\s+|readonly\s+)*"
                        r"([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;]*\)\s*(?::\s*[^{]+)?\{\s*$")
_JS_IMPORT = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(?\s*|require\(\s*|export\s[^'"]*?from\s*)['"]([^'"]+)['"]""")

_JAVA_TYPE = re.compile(r"^\s*(?:public|private|protected|abstract|final|static|sealed|\s)*(class|interface|enum|record)\s+(\w+)")
_JAVA_METHOD = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:public|private|protected|static|final|abstract|synchronized|native|default|\s)+"
                          r"[\w<>\[\],.?\s]+\s+(\w+)\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{?\s*$")
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;")

_GO_FUNC = re.compile(r"^func\s+(?:\((\w+)\s+\*?([\w\[\]]+)\)\s+)?(\w+)\s*[\[(]")
_GO_TYPE = re.compile(r"^type\s+(\w+)\s+(struct|interface)\b")
_GO_IMPORT_SINGLE = re.compile(r'^import\s+(?:\w+\s+)?"([^"]+)"')
_GO_IMPORT_LINE = re.compile(r'^\s+(?:\w+\s+|_\s+|\.\s+)?"([^"]+)"')

_RUST_ITEM = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(fn|struct|enum|trait|mod)\s+(\w+)")
_RUST_IMPL = re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([\w:]+)")
_RUST_USE = re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)")

_C_FUNC = re.compile(r"^(?!\s)(?:[\w\*&:<>,]+\s+)+\**&?([A-Za-z_][\w:~]*)\s*\([^;{]*\)\s*(?:const)?\s*(?:noexcept)?\s*\{?\s*$")
_C_TYPE = re.compile(r"^\s*(?:typedef\s+)?(struct|class|union|enum)\s+(\w+)\s*(?::[^{]*)?\{?\s*$")
_C_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')

_SQL_OBJECT = re.compile(
    r"(?im)^\s*create\s+(?:or\s+replace\s+)?(?:temporary\s+|temp\s+|unique\s+|materialized\s+)*"
    r"(table|view|function|procedure|index|trigger|type|sequence)\s+(?:if\s+not\s+exists\s+)?([\w.\"`\[\]]+)"
)
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _extract_js(lines: list[str]) -> ExtractionResult:
    result = ExtractionResult()
    current_class: tuple[str, int] | None = None
    for idx, line in enumerate(lines):
        if current_class and idx > current_class[1]:
            current_class = None
        matched = False
        for kind, pattern in _JS_PATTERNS:
            m = pattern.match(line)
            if m:
                end = _block_end(lines, idx) if "{" in "".join(lines[idx:idx + 3]) else idx
                if kind == "type":
                    end = idx
                    while end < len(lines) - 1 and not lines[end].rstrip().endswith(";") and end - idx < 50:
                        end += 1
                result.symbols.append(SymbolInfo(m.group(1), kind, idx + 1, end + 1, line.strip()[:300],
                                                 current_class[0] if current_class and kind == "function" and
                                                 line.startswith((" ", "\t")) else None))
                if kind == "class":
                    current_class = (m.group(1), end)
                matched = True
                break
        if not matched and current_class:
            m = _JS_METHOD.match(line)
            if m and m.group(1) not in _CONTROL:
                end = _block_end(lines, idx)
                result.symbols.append(SymbolInfo(m.group(1), "method", idx + 1, end + 1, line.strip()[:300],
                                                 current_class[0]))
    text = "\n".join(lines)
    result.imports = sorted(set(_JS_IMPORT.findall(text)))
    return result


def _extract_java(lines: list[str]) -> ExtractionResult:
    result = ExtractionResult()
    stack: list[tuple[str, int]] = []
    for idx, line in enumerate(lines):
        while stack and idx > stack[-1][1]:
            stack.pop()
        m = _JAVA_IMPORT.match(line)
        if m:
            result.imports.append(m.group(1))
            continue
        m = _JAVA_TYPE.match(line)
        if m:
            end = _block_end(lines, idx)
            result.symbols.append(SymbolInfo(m.group(2), m.group(1), idx + 1, end + 1, line.strip()[:300],
                                             stack[-1][0] if stack else None))
            stack.append((m.group(2), end))
            continue
        m = _JAVA_METHOD.match(line)
        if m and stack and m.group(1) not in _CONTROL:
            end = _block_end(lines, idx)
            kind = "constructor" if m.group(1) == stack[-1][0] else "method"
            result.symbols.append(SymbolInfo(m.group(1), kind, idx + 1, end + 1, line.strip()[:300], stack[-1][0]))
    return result


def _extract_go(lines: list[str]) -> ExtractionResult:
    result = ExtractionResult()
    in_import = False
    for idx, line in enumerate(lines):
        if in_import:
            if line.strip().startswith(")"):
                in_import = False
                continue
            m = _GO_IMPORT_LINE.match(line)
            if m:
                result.imports.append(m.group(1))
            continue
        if line.startswith("import ("):
            in_import = True
            continue
        m = _GO_IMPORT_SINGLE.match(line)
        if m:
            result.imports.append(m.group(1))
            continue
        m = _GO_FUNC.match(line)
        if m:
            end = _block_end(lines, idx)
            receiver = m.group(2)
            result.symbols.append(SymbolInfo(m.group(3), "method" if receiver else "function", idx + 1, end + 1,
                                             line.strip()[:300], receiver.strip("*") if receiver else None))
            continue
        m = _GO_TYPE.match(line)
        if m:
            end = _block_end(lines, idx)
            result.symbols.append(SymbolInfo(m.group(1), m.group(2), idx + 1, end + 1, line.strip()[:300]))
    return result


def _extract_rust(lines: list[str]) -> ExtractionResult:
    result = ExtractionResult()
    impl: tuple[str, int] | None = None
    for idx, line in enumerate(lines):
        if impl and idx > impl[1]:
            impl = None
        m = _RUST_USE.match(line)
        if m:
            result.imports.append(m.group(1))
            continue
        m = _RUST_IMPL.match(line)
        if m and line.strip().startswith("impl"):
            end = _block_end(lines, idx)
            result.symbols.append(SymbolInfo(m.group(1), "impl", idx + 1, end + 1, line.strip()[:300]))
            impl = (m.group(1), end)
            continue
        m = _RUST_ITEM.match(line)
        if m:
            end = _block_end(lines, idx)
            kind = "method" if m.group(1) == "fn" and impl else ("function" if m.group(1) == "fn" else m.group(1))
            result.symbols.append(SymbolInfo(m.group(2), kind, idx + 1, end + 1, line.strip()[:300],
                                             impl[0] if impl and m.group(1) == "fn" else None))
    return result


def _extract_c(lines: list[str]) -> ExtractionResult:
    result = ExtractionResult()
    for idx, line in enumerate(lines):
        m = _C_INCLUDE.match(line)
        if m:
            result.imports.append(m.group(1))
            continue
        m = _C_TYPE.match(line)
        if m:
            end = _block_end(lines, idx)
            if end > idx:
                result.symbols.append(SymbolInfo(m.group(2), m.group(1), idx + 1, end + 1, line.strip()[:300]))
            continue
        m = _C_FUNC.match(line)
        if m and m.group(1).split("::")[-1] not in _CONTROL:
            nxt = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if line.rstrip().endswith("{") or nxt.startswith("{"):
                end = _block_end(lines, idx)
                name = m.group(1)
                parent = name.rsplit("::", 1)[0] if "::" in name else None
                result.symbols.append(SymbolInfo(name.rsplit("::", 1)[-1], "method" if parent else "function",
                                                 idx + 1, end + 1, line.strip()[:300], parent))
    return result


def _extract_sql(text: str) -> ExtractionResult:
    result = ExtractionResult()
    for m in _SQL_OBJECT.finditer(text):
        start_line = text.count("\n", 0, m.start()) + 1
        end_pos = text.find(";", m.end())
        end_line = text.count("\n", 0, end_pos) + 1 if end_pos != -1 else start_line
        name = m.group(2).strip('"`[]')
        result.symbols.append(SymbolInfo(name, m.group(1).lower(), start_line, end_line,
                                         text[m.start():m.end()].strip()[:300]))
    return result


def _extract_markdown(lines: list[str]) -> ExtractionResult:
    result = ExtractionResult()
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    for idx, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        m = None if in_fence else _MD_HEADING.match(line)
        if m:
            headings.append((idx, len(m.group(1)), m.group(2)))
    for i, (idx, level, title) in enumerate(headings):
        end = len(lines) - 1
        for j in range(i + 1, len(headings)):
            if headings[j][1] <= level:
                end = headings[j][0] - 1
                break
        result.symbols.append(SymbolInfo(title[:200], "section", idx + 1, end + 1, "#" * level + " " + title))
    return result


def _extract_top_level_keys(lines: list[str], language: str) -> ExtractionResult:
    result = ExtractionResult()
    key = re.compile(r'^([A-Za-z_][\w.\-]*)\s*:' if language == "yaml" else r'^\s{2}"([^"]+)"\s*:')
    keys: list[tuple[int, str]] = [(i, m.group(1)) for i, line in enumerate(lines) if (m := key.match(line))]
    for n, (idx, name) in enumerate(keys):
        end = keys[n + 1][0] - 1 if n + 1 < len(keys) else len(lines) - 1
        result.symbols.append(SymbolInfo(name, "key", idx + 1, max(idx, end) + 1, lines[idx].strip()[:200]))
    return result


def extract_symbols(language: str | None, text: str) -> ExtractionResult:
    if not language or not text:
        return ExtractionResult()
    lines = text.splitlines()
    try:
        if language == "python":
            return _extract_python(text)
        if language in {"javascript", "jsx", "typescript", "tsx", "vue", "svelte"}:
            return _extract_js(lines)
        if language in {"java", "kotlin", "csharp", "scala"}:
            return _extract_java(lines)
        if language == "go":
            return _extract_go(lines)
        if language == "rust":
            return _extract_rust(lines)
        if language in {"c", "cpp"}:
            return _extract_c(lines)
        if language == "sql":
            return _extract_sql(text)
        if language == "markdown":
            return _extract_markdown(lines)
        if language in {"yaml", "json"}:
            return _extract_top_level_keys(lines, language)
    except (RecursionError, ValueError) as exc:  # pathological input must not break indexing
        return ExtractionResult(parse_error=f"{type(exc).__name__}: {exc}")
    return ExtractionResult()
