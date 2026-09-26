"""Execution modes. Users can pick one; "auto" chooses from intent/complexity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Mode(StrEnum):
    AUTO = "auto"
    QUICK = "quick"
    DEVELOPER = "developer"
    DEEP = "deep"
    PROJECT = "project"
    DEBUG = "debug"
    REVIEW = "review"
    ARCHITECTURE = "architecture"


@dataclass(frozen=True)
class ModePolicy:
    mode: Mode
    use_llm_planner: bool
    critic: bool
    run_tests: bool
    lint: bool
    max_fix_iterations: int
    max_steps: int
    forced_intent: str | None = None
    extra_review: bool = False  # add security/code review of produced changes


POLICIES: dict[Mode, ModePolicy] = {
    Mode.QUICK: ModePolicy(Mode.QUICK, use_llm_planner=False, critic=False, run_tests=False, lint=False,
                           max_fix_iterations=0, max_steps=3),
    Mode.DEVELOPER: ModePolicy(Mode.DEVELOPER, use_llm_planner=False, critic=True, run_tests=True, lint=True,
                               max_fix_iterations=1, max_steps=6),
    Mode.DEEP: ModePolicy(Mode.DEEP, use_llm_planner=True, critic=True, run_tests=True, lint=True,
                          max_fix_iterations=2, max_steps=10, extra_review=True),
    Mode.PROJECT: ModePolicy(Mode.PROJECT, use_llm_planner=False, critic=True, run_tests=True, lint=True,
                             max_fix_iterations=1, max_steps=8),
    Mode.DEBUG: ModePolicy(Mode.DEBUG, use_llm_planner=False, critic=True, run_tests=True, lint=True,
                           max_fix_iterations=2, max_steps=6, forced_intent="debug"),
    Mode.REVIEW: ModePolicy(Mode.REVIEW, use_llm_planner=False, critic=True, run_tests=False, lint=True,
                            max_fix_iterations=0, max_steps=4, forced_intent="review"),
    Mode.ARCHITECTURE: ModePolicy(Mode.ARCHITECTURE, use_llm_planner=False, critic=True, run_tests=False, lint=False,
                                  max_fix_iterations=0, max_steps=4, forced_intent="architecture"),
}


def resolve_mode(requested: str, *, intent: str, complexity: str, has_project: bool) -> ModePolicy:
    try:
        mode = Mode(requested)
    except ValueError:
        mode = Mode.AUTO
    if mode != Mode.AUTO:
        return POLICIES[mode]
    if complexity == "trivial" or (not has_project and complexity in ("simple", "moderate")):
        return POLICIES[Mode.QUICK]
    if complexity == "complex":
        return POLICIES[Mode.DEEP]
    return POLICIES[Mode.DEVELOPER]
