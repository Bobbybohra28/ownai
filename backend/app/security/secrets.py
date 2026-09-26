"""Secret detection and masking.

This module is dependency-free (stdlib only) so it can be used from logging,
indexing, tools, memory and prompt assembly alike. Detection combines well-known
token formats with key/value heuristics; it is intentionally conservative about
*masking* (better to over-redact a prompt than leak a credential).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import PurePosixPath

REDACTED = "[REDACTED]"


@dataclass(frozen=True, slots=True)
class SecretRule:
    id: str
    pattern: re.Pattern[str]
    # index of the group containing the secret value (0 = whole match)
    group: int = 0


_RULES: tuple[SecretRule, ...] = (
    SecretRule(
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
            r"(?:-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----|\Z)"
        ),
    ),
    SecretRule("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    SecretRule("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{60,255})\b")),
    SecretRule("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    SecretRule("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    SecretRule("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    SecretRule("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    SecretRule("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    SecretRule("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    SecretRule("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    SecretRule(
        "url_credentials",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]{1,20}://[^\s:/@'\"]{1,128}:([^\s@/'\"]{3,256})@[^\s'\"]+"),
        group=1,
    ),
    SecretRule(
        "assignment",
        re.compile(
            r"(?i)\b[\w.\-]*(?:password|passwd|pwd|secret|api[_\-]?key|apikey|access[_\-]?key|"
            r"auth[_\-]?token|access[_\-]?token|refresh[_\-]?token|client[_\-]?secret|private[_\-]?key|"
            r"bearer)[\w.\-]*\s*[:=]\s*(?:[\"']([^\"'\n]{6,512})[\"']|([^\s\"'#,;]{8,512}))"
        ),
        group=-1,  # special: whichever of group 1/2 matched
    ),
    SecretRule("authorization_header", re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer|basic|token)\s+([A-Za-z0-9._~+/=\-]{8,})"), group=1),
)

# Values that look like assignments but are clearly not secrets.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:\$\{?[\w:\-]+\}?|<[^>]+>|\{\{[^}]+\}\}|changeme|change-me|example|your[_\-].*|xxx+|\*+|"
    r"none|null|true|false|os\.environ.*|os\.getenv.*|getenv.*|process\.env.*|settings\..*|config\..*|"
    r"env\(.*|str|string|int|bool|password|secret|optional\[.*|secretstr.*|field\(.*)$"
)


@dataclass(frozen=True, slots=True)
class SecretFinding:
    rule: str
    line: int
    start: int
    end: int
    preview: str  # masked preview, safe to display


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _secret_span(rule: SecretRule, match: re.Match[str]) -> tuple[int, int] | None:
    if rule.group == -1:
        for idx in (1, 2):
            if match.group(idx):
                value = match.group(idx)
                if _PLACEHOLDER.match(value.strip()):
                    return None
                # identifiers such as `password = user_password` are code, not secrets
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*(?:\(\))?", value) and _shannon_entropy(value) < 3.5:
                    return None
                return match.span(idx)
        return None
    if rule.group:
        value = match.group(rule.group)
        if _PLACEHOLDER.match(value):
            return None
        return match.span(rule.group)
    return match.span(0)


def find_secrets(text: str) -> list[SecretFinding]:
    """Return non-overlapping secret findings with 1-based line numbers."""
    spans: list[tuple[int, int, str]] = []
    for rule in _RULES:
        for match in rule.pattern.finditer(text):
            span = _secret_span(rule, match)
            if span is None:
                continue
            if any(s <= span[0] < e or s < span[1] <= e for s, e, _ in spans):
                continue
            spans.append((span[0], span[1], rule.id))
    spans.sort()
    findings: list[SecretFinding] = []
    for start, end, rule_id in spans:
        line = text.count("\n", 0, start) + 1
        value = text[start:end]
        preview = (value[:4] + "…" + REDACTED) if len(value) > 12 and rule_id != "private_key" else REDACTED
        findings.append(SecretFinding(rule=rule_id, line=line, start=start, end=end, preview=preview))
    return findings


def mask_secrets(text: str) -> str:
    """Replace every detected secret value with a redaction marker."""
    if not text:
        return text
    findings = find_secrets(text)
    if not findings:
        return text
    out: list[str] = []
    cursor = 0
    for f in findings:
        out.append(text[cursor : f.start])
        # keep the original line structure so line-number citations stay correct
        out.append(f"[REDACTED:{f.rule}]" + "\n" * text.count("\n", f.start, f.end))
        cursor = f.end
    out.append(text[cursor:])
    return "".join(out)


def contains_secret(text: str) -> bool:
    return bool(text) and bool(find_secrets(text))


# --- sensitive files --------------------------------------------------------------------------

SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".pgpass",
    ".pypirc",
    ".npmrc",
    ".dockercfg",
    "credentials",
    "credentials.json",
    "credentials.*",
    "*secret*.json",
    "*secrets*.y*ml",
    "service-account*.json",
    "*.tfstate",
    "*.tfstate.*",
)
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")


def is_sensitive_file(path: str) -> bool:
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    if name.startswith(".env") and name.endswith(_ENV_TEMPLATE_SUFFIXES):
        return False
    return any(fnmatch(name, pat) for pat in SENSITIVE_FILE_PATTERNS)


def is_env_file(path: str) -> bool:
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    return name == ".env" or name.startswith(".env.")


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*=", re.MULTILINE)


def list_env_keys(text: str) -> list[str]:
    """Return variable names from a dotenv-style file without their values."""
    return sorted(set(_ENV_LINE.findall(text)))


_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_\-]?key|apikey|authorization|cookie|private[_\-]?key|"
    r"credential|access[_\-]?key|session[_\-]?id)"
)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def redact_mapping(data: object, *, depth: int = 0) -> object:
    """Recursively redact values under sensitive keys and mask secrets in strings."""
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(data, dict):
        return {
            k: (REDACTED if isinstance(k, str) and is_sensitive_key(k) and v not in (None, "") else
                redact_mapping(v, depth=depth + 1))
            for k, v in data.items()
        }
    if isinstance(data, list | tuple):
        return [redact_mapping(v, depth=depth + 1) for v in data]
    if isinstance(data, str):
        return mask_secrets(data)
    return data
