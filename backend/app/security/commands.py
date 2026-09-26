"""Command restrictions.

Tools never pass shell strings: commands are argv lists validated against a
per-profile allowlist. ``find_dangerous_patterns`` flags destructive intent in
free text (scripts, SQL, commands proposed by a model) so it can be surfaced or
routed to human approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.core.exceptions import ErrorCode, PermissionDenied

# Executables allowed inside the sandbox, per runtime profile.
PROFILE_ALLOWLIST: dict[str, frozenset[str]] = {
    "python": frozenset({"python", "python3", "pytest", "ruff", "pip"}),
    "node": frozenset({"node", "npm", "npx"}),
    "go": frozenset({"go"}),
    "rust": frozenset({"cargo"}),
}

# Sub-commands that are never allowed even for allowlisted executables.
_DENIED_ARGS: dict[str, tuple[tuple[str, ...], ...]] = {
    "pip": (("install",), ("uninstall",), ("download",)),  # network/dependency changes need approval flow
    "npm": (("install",), ("i",), ("publish",), ("exec",)),
    "npx": ((),),  # npx fetches packages from the network
    "go": (("get",), ("install",)),
    "cargo": (("install",), ("publish",)),
}

_SHELL_META = re.compile(r"[;&|`$<>]|\$\(")


@dataclass(frozen=True, slots=True)
class DangerFinding:
    rule: str
    description: str
    match: str


def validate_argv(argv: list[str], profile: str) -> list[str]:
    if not argv:
        raise PermissionDenied("Empty command.", code=ErrorCode.COMMAND_NOT_ALLOWED)
    allowed = PROFILE_ALLOWLIST.get(profile)
    if allowed is None:
        raise PermissionDenied(f"Unknown sandbox profile '{profile}'.", code=ErrorCode.COMMAND_NOT_ALLOWED)
    exe = PurePosixPath(argv[0]).name
    if exe not in allowed:
        raise PermissionDenied(
            f"'{exe}' is not an allowed command in the {profile} sandbox. Allowed: {', '.join(sorted(allowed))}.",
            code=ErrorCode.COMMAND_NOT_ALLOWED,
        )
    for denied in _DENIED_ARGS.get(exe, ()):
        if not denied or (len(argv) > len(denied) and tuple(argv[1 : 1 + len(denied)]) == denied):
            raise PermissionDenied(f"'{' '.join(argv[:3])}' is not allowed in the sandbox.",
                                   code=ErrorCode.COMMAND_NOT_ALLOWED)
    for arg in argv:
        if "\x00" in arg or len(arg) > 4096:
            raise PermissionDenied("Invalid command argument.", code=ErrorCode.COMMAND_NOT_ALLOWED)
    return [exe, *argv[1:]]


def looks_like_shell(command: str) -> bool:
    return bool(_SHELL_META.search(command))


_DANGEROUS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("rm_rf_root", "Recursive deletion of root/home directories",
     re.compile(r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|--recursive\s+--force)\s+(?:/|~|\$HOME|\*)(?:\s|$)")),
    ("rm_rf", "Recursive forced deletion", re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r")),
    ("mkfs", "Filesystem formatting", re.compile(r"\bmkfs(\.\w+)?\b")),
    ("dd_device", "Raw disk write", re.compile(r"\bdd\s+[^\n]*of=/dev/")),
    ("fork_bomb", "Fork bomb", re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:")),
    ("pipe_to_shell", "Downloading and executing remote code", re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")),
    ("chmod_777", "World-writable permissions", re.compile(r"\bchmod\s+(-R\s+)?777\b")),
    ("shutdown", "Host shutdown/reboot", re.compile(r"\b(shutdown|reboot|halt|poweroff)\b(\s|$)")),
    ("sudo", "Privilege escalation", re.compile(r"\bsudo\s+")),
    ("device_write", "Writing to a block device", re.compile(r">\s*/dev/sd[a-z]")),
    ("python_shell", "Python spawning a shell", re.compile(r"os\.system\(|subprocess\.[a-zA-Z_]+\([^)]*shell\s*=\s*True")),
    ("git_force_push", "Force-pushing Git history", re.compile(r"\bgit\s+push\b[^\n]*(--force|-f\b)")),
    ("git_reset_hard", "Discarding uncommitted work", re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f)")),
    ("docker_privileged", "Privileged container", re.compile(r"--privileged\b")),
    ("docker_socket", "Docker socket access", re.compile(r"/var/run/docker\.sock")),
    ("kubectl_delete", "Deleting Kubernetes resources", re.compile(r"\bkubectl\s+delete\b")),
    ("terraform_destroy", "Destroying infrastructure", re.compile(r"\bterraform\s+destroy\b")),
    ("sql_drop", "Dropping database objects", re.compile(r"(?i)\bdrop\s+(table|database|schema|index|view)\b")),
    ("sql_truncate", "Truncating tables", re.compile(r"(?i)\btruncate\s+(table\s+)?\w+")),
)


def find_dangerous_patterns(text: str) -> list[DangerFinding]:
    findings: list[DangerFinding] = []
    seen: set[str] = set()
    for rule, description, pattern in _DANGEROUS:
        match = pattern.search(text or "")
        if match and rule not in seen:
            if rule == "rm_rf" and "rm_rf_root" in seen:
                continue
            seen.add(rule)
            findings.append(DangerFinding(rule=rule, description=description, match=match.group(0)[:120]))
    return findings
