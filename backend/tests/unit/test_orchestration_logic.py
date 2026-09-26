"""Pure-logic tests for agents/orchestrator pieces (no external services)."""

from pathlib import Path

import pytest

from app.agents.builtin.responder import compose_report, guard_claims
from app.agents.json_output import compact_schema, extract_json_object
from app.agents.registry import AgentRegistry
from app.agents.schemas import CriticVerdict, PlanOutput, PlanStepOut
from app.core.exceptions import OrchestratorError
from app.orchestrator.evidence import EvidenceItem, EvidenceLedger
from app.orchestrator.intent import estimate_complexity, rule_intent
from app.orchestrator.modes import Mode, resolve_mode
from app.orchestrator.plan import template_plan, validate_planner_output
from app.orchestrator.verification import syntax_errors
from app.tools.changeset import recover_path, unified_diff, whitespace_tolerant_replace
from app.tools.registry import ToolRegistry
from app.tools.sql import classify_sql

AGENTS_DIR = Path(__file__).resolve().parents[3] / "config" / "agents"


@pytest.fixture(scope="module")
def agents() -> AgentRegistry:
    return AgentRegistry.from_directory(AGENTS_DIR, ToolRegistry())


@pytest.mark.parametrize("text,intent", [
    ("Explain how authentication works in my project.", "explain"),
    ("Find the authentication bug and fix it.", "fix_bug"),
    ("Why is test_login failing? Here is the traceback", "debug"),
    ("Write unit tests for the parser", "write_tests"),
    ("Review my code for problems", "review"),
    ("Scan the project for hardcoded secrets", "security"),
    ("Write a SQL query that lists customers without orders", "sql"),
    ("Show the git log for the last week", "git"),
])
def test_rule_intents(text, intent):
    assert rule_intent(text, True)[0] == intent


def test_complexity_and_modes():
    assert estimate_complexity("hi") == "trivial"
    assert estimate_complexity("Refactor the entire project architecture for scalability and then migrate") == "complex"
    assert resolve_mode("auto", intent="explain", complexity="trivial", has_project=True).mode == Mode.QUICK
    assert resolve_mode("auto", intent="fix_bug", complexity="complex", has_project=True).mode == Mode.DEEP
    assert resolve_mode("debug", intent="x", complexity="simple", has_project=True).forced_intent == "debug"


def test_agent_specs_valid(agents: AgentRegistry):
    ids = {s.id for s in agents.specs()}
    required = {"planner", "coding", "debugging", "project_context", "critic", "responder", "code_review", "refactoring",
                "testing", "architecture", "documentation", "sql", "git", "docker", "devops", "security", "rag",
                "research", "data_analysis"}
    assert required <= ids
    for spec in agents.specs():
        agent = agents.get(spec.id)
        assert agent.spec.description and agent.spec.model_role


def test_template_plans(agents: AgentRegistry):
    plan = template_plan("fix_bug", has_project=True, registry=agents, critic=True)
    assert [s.agent for s in plan.steps] == ["project_context", "debugging", "coding"]
    assert plan.steps[2].produces_changes
    assert [s.agent for s in template_plan("explain", has_project=False, registry=agents, critic=True).steps] == ["assistant"]


def test_planner_output_validation(agents: AgentRegistry):
    good = PlanOutput(steps=[PlanStepOut(id="a", agent="project_context", goal="g"),
                             PlanStepOut(id="b", agent="rag", goal="g", depends_on=["a"])])
    plan = validate_planner_output(good, intent="explain", registry=agents, has_project=True, max_steps=5, critic=True)
    assert [len(layer) for layer in plan.order()] == [1, 1]
    for bad in (
        PlanOutput(steps=[PlanStepOut(id="a", agent="does_not_exist", goal="g")]),
        PlanOutput(steps=[PlanStepOut(id="a", agent="rag", goal="g", depends_on=["b"])]),
        PlanOutput(steps=[PlanStepOut(id="a", agent="critic", goal="g")]),  # core agents are not plannable
    ):
        with pytest.raises(OrchestratorError):
            validate_planner_output(bad, intent="explain", registry=agents, has_project=True, max_steps=5, critic=True)


def test_json_extraction():
    assert extract_json_object('Sure!\n```json\n{"a": 1,}\n```') == {"a": 1}
    assert extract_json_object('text {"x": {"y": "}"}} tail') == {"x": {"y": "}"}}
    assert extract_json_object("no json here") is None
    skeleton = compact_schema(CriticVerdict.model_json_schema())
    assert skeleton["verdict"] == "pass | fail | uncertain"


def test_claims_guard_and_report():
    ledger = EvidenceLedger()
    text, guarded = guard_claims("I fixed it and all tests pass.", ledger)
    assert guarded and "unverified" in text
    ledger.add(EvidenceItem(kind="tests", status="passed", summary="applied: All tests passed",
                            data={"phase": "applied", "applied": True}))
    assert guard_claims("All tests pass.", ledger) == ("All tests pass.", False)
    report_text, report = compose_report(request="q", primary={"text": "Answer [S1]"}, ledger=ledger,
                                         citations=[{"id": "S1", "file_path": "a.py", "start_line": 1, "end_line": 5,
                                                     "symbol": "f"}],
                                         critic={"verdict": "pass", "summary": "ok", "issues": []}, changeset=None,
                                         notices=["note"], approval=None)
    assert "### Sources" in report_text and "a.py" in report_text and "After applying changes" in report_text
    assert report["verification"]["verdict"] == "pass"


def test_edit_helpers(tmp_path: Path):
    src = "def f():\n    if a < b:\n        raise E()\n    return 1\n"
    out = whitespace_tolerant_replace(src, "if a < b:\n    raise E()", "if a > b:\n    raise E()")
    assert out == "def f():\n    if a > b:\n        raise E()\n    return 1\n"
    assert whitespace_tolerant_replace(src, "missing", "x") is None
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "auth.py").write_text("x")
    assert recover_path(tmp_path, "pkg/auth.py/pkg/auth.py") == "pkg/auth.py"
    assert recover_path(tmp_path, "auth.py") == "pkg/auth.py"
    diff, added, removed = unified_diff("a.py", "x\n", "y\n")
    assert added == 1 and removed == 1 and "--- a/a.py" in diff


def test_syntax_check():
    assert syntax_errors({"a.py": "def f(:\n", "b.json": "{}", "c.py": None}) and not syntax_errors({"a.py": "x = 1\n"})


@pytest.mark.parametrize("sql,category", [
    ("SELECT * FROM users", "read"),
    ("WITH t AS (SELECT 1) SELECT * FROM t", "read"),
    ("DELETE FROM users", "write"),
    ("UPDATE users SET a = 1 WHERE id = 2", "write"),
    ("DROP TABLE users", "ddl"),
    ("TRUNCATE users", "ddl"),
    ("ALTER TABLE users ADD COLUMN x int", "ddl"),
    ("SELECT 1; DROP TABLE x", "ddl"),
    ("GRANT ALL ON users TO bob", "admin"),
])
def test_sql_classification(sql, category):
    c = classify_sql(sql)
    assert c.category == category
    if sql == "DELETE FROM users":
        assert any("WHERE" in w for w in c.warnings)
