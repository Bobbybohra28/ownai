"""Full-stack API tests: real FastAPI app + PostgreSQL + Qdrant (+ sandbox), fake model server."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from tests.integration.conftest import ApiClient, parse_sse, wait_for

requires_sandbox = pytest.mark.skipif(not os.environ.get("OWNAI_TEST_SANDBOX_URL"),
                                      reason="set OWNAI_TEST_SANDBOX_URL to run sandbox-backed tests")


async def import_fixture(client: ApiClient, fixtures_root, name: str = "TaskBoard") -> str:
    r = await client.request("POST", "/projects/import/local", expect=201,
                             json={"name": name, "path": str(fixtures_root / "sample_project")})
    pid = r.json()["id"]

    async def ready():
        s = (await client.request("GET", f"/projects/{pid}/index-status")).json()
        assert s["status"] != "error", s
        return s if s["status"] == "ready" else None

    status = await wait_for(ready)
    assert status["job"]["status"] in ("completed", "completed_with_warnings")
    return pid


async def run_to_end(client: ApiClient, conversation_id: str, content: str, mode: str = "developer") -> dict:
    sub = await client.request("POST", f"/conversations/{conversation_id}/messages", expect=202,
                               json={"content": content, "mode": mode})
    run_id = sub.json()["run_id"]
    stream = await client.request("GET", f"/runs/{run_id}/events", expect=200)
    events = parse_sse(stream.text)
    run = (await client.request("GET", f"/runs/{run_id}", expect=200)).json()
    return {"run_id": run_id, "events": events, "run": run}


# --------------------------------------------------------------------------------------------------
async def test_auth_lifecycle(app):
    c = ApiClient(app)
    user = await c.register()
    me = (await c.request("GET", "/auth/me", expect=200)).json()
    assert me["user"]["email"] == user["user"]["email"] and me["role"] == "owner"
    refreshed = await c.request("POST", "/auth/refresh", expect=200)
    assert refreshed.json()["access_token"]
    bad = await c.request("POST", "/auth/login", json={"email": user["user"]["email"], "password": "nope"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "INVALID_CREDENTIALS"
    weak = await c.request("POST", "/auth/register", json={"email": "weak@example.com", "password": "short"})
    assert weak.status_code == 422
    c.token = None
    anon = await c.request("GET", "/projects")
    assert anon.status_code == 401 and anon.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_model_health_is_real(client, fake):
    health = (await client.request("GET", "/models/health", params={"refresh": "true"}, expect=200)).json()
    statuses = {m["model_id"]: m["status"] for m in health["models"]}
    assert statuses == {"chat": "online", "embed": "online"}
    await fake.post("/_control", json={"mode": "empty"})
    report = (await client.request("POST", "/models/chat/test", expect=200)).json()
    assert report["status"] == "offline" and report["error_code"] == "MODEL_EMPTY_RESPONSE"
    listed = (await client.request("GET", "/models", expect=200)).json()
    assert next(m for m in listed["models"] if m["id"] == "chat")["online"] is False
    await fake.post("/_control", json={"mode": "normal"})
    report = (await client.request("POST", "/models/chat/test", expect=200)).json()
    assert report["status"] == "online" and any(s["name"] == "streaming" and s["ok"] for s in report["steps"])


async def test_project_indexing_rag_and_isolation(app, client, fixtures_root):
    pid = await import_fixture(client, fixtures_root)
    project = (await client.request("GET", f"/projects/{pid}", expect=200)).json()
    ov = project["overview"]
    assert ov["languages"]["python"] >= 5 and ov["tests"]["framework"] == "pytest"
    assert ".env" in ov["sensitive_files"] and ov["env_files"][".env"] == ["DATABASE_URL", "TASKBOARD_SECRET"]
    env = (await client.request("GET", f"/projects/{pid}/files/content", params={"path": ".env"}, expect=200)).json()
    assert env["content"] is None and "TASKBOARD_SECRET" in env["variables"]
    traversal = await client.request("GET", f"/projects/{pid}/files/content", params={"path": "../../etc/passwd"})
    assert traversal.status_code == 403
    search = (await client.request("POST", "/rag/search", expect=200,
                                   json={"query": "how is the token signature verified", "project_id": pid})).json()
    assert search["semantic"] and search["lexical"]
    assert any(r["file_path"] == "taskboard/auth.py" for r in search["results"])
    other = ApiClient(app)
    await other.register()
    assert (await other.request("GET", f"/projects/{pid}")).status_code == 404
    assert pid not in {p["id"] for p in (await other.request("GET", "/projects", expect=200)).json()}


async def test_chat_explain_flow_streams_progress(client, fixtures_root, fake):
    pid = await import_fixture(client, fixtures_root)
    conv = (await client.request("POST", "/conversations", expect=201, json={"project_id": pid})).json()
    result = await run_to_end(client, conv["id"], "Explain how authentication works in my project.")
    types = [t for t, _ in result["events"]]
    for expected in ("run_started", "intent", "plan", "step_started", "model_selected", "token", "answer", "run_finished"):
        assert expected in types, (expected, types)
    agents = [d["agent"] for t, d in result["events"] if t == "step_started"]
    assert agents[:2] == ["project_context", "rag"]
    assert result["run"]["status"] == "succeeded"
    messages = (await client.request("GET", f"/conversations/{conv['id']}/messages", expect=200)).json()
    final = messages[-1]
    assert final["role"] == "assistant" and final["content"].strip()
    assert "taskboard/" in final["content"]  # sources listed
    report = final["meta"]["report"]
    assert report["citations"] and report["models"] == ["chat"]


async def test_empty_model_output_is_reported_not_hidden(client, fake):
    await fake.post("/_control", json={"mode": "empty"})
    conv = (await client.request("POST", "/conversations", expect=201, json={})).json()
    result = await run_to_end(client, conv["id"], "What is a Python decorator?", mode="quick")
    assert result["run"]["status"] == "failed"
    assert result["run"]["error_code"] and result["run"]["error_code"].startswith(("MODEL_", "AGENT_"))
    assert any(t == "error" for t, _ in result["events"])
    messages = (await client.request("GET", f"/conversations/{conv['id']}/messages", expect=200)).json()
    assert "could not be completed" in messages[-1]["content"]


async def test_pipeline_diagnostics(client, fake):
    report = (await client.request("POST", "/admin/diagnostics/pipeline", expect=200)).json()
    assert report["ok"], report
    await fake.post("/_control", json={"mode": "invalid_json"})
    report = (await client.request("POST", "/admin/diagnostics/pipeline", expect=200)).json()
    assert not report["ok"] and report["first_failure"].startswith("model:")
    failing = next(s for s in report["steps"] if not s["ok"])
    assert failing["error_code"] == "MODEL_INVALID_RESPONSE"


async def test_role_permissions_and_tool_policy(app, client, fixtures_root):
    pid = await import_fixture(client, fixtures_root)
    viewer = ApiClient(app)
    info = await viewer.register()
    await client.request("POST", "/orgs/current/members", expect=201,
                         json={"email": info["user"]["email"], "role": "viewer"})
    orgs = (await viewer.request("GET", "/orgs", expect=200)).json()
    target = next(o for o in orgs if not o["current"])
    switched = (await viewer.request("POST", f"/orgs/{target['id']}/switch", expect=200)).json()
    viewer.token = switched["access_token"]
    denied = (await viewer.request("POST", "/tools/run_python/invoke", expect=200,
                                   json={"project_id": pid, "args": {"code": "print(1)"}})).json()
    assert denied["status"] == "denied" and denied["error_code"] == "PERMISSION_DENIED"
    create = await viewer.request("POST", "/projects", json={"name": "nope"})
    assert create.status_code == 403
    readable = (await viewer.request("POST", "/tools/read_file/invoke", expect=200,
                                     json={"project_id": pid, "args": {"path": "taskboard/auth.py"}})).json()
    assert readable["status"] == "ok"
    escape = (await client.request("POST", "/tools/read_file/invoke", expect=200,
                                   json={"project_id": pid, "args": {"path": "../../../etc/passwd"}})).json()
    assert escape["status"] == "denied"
    audit = (await client.request("GET", "/admin/audit-logs", expect=200)).json()
    assert any(a["action"] == "tool.denied" for a in audit)


async def test_sql_read_and_write_approval(client, fixtures_root, container):
    pid = await import_fixture(client, fixtures_root, name="SQL project")
    project = (await client.request("GET", f"/projects/{pid}", expect=200)).json()
    from pathlib import Path

    root = Path(container.settings.projects_root)
    db_path = next(root.rglob(f"{pid}"))/"data.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO users VALUES (1,'a'),(2,'b');")
    conn.close()
    assert project["name"] == "SQL project"
    c = (await client.request("POST", f"/projects/{pid}/db-connections", expect=201,
                              json={"name": "local", "dialect": "sqlite", "database": "data.db", "read_only": False})).json()
    read = (await client.request("POST", f"/db-connections/{c['id']}/query", expect=200,
                                 json={"query": "SELECT count(*) AS n FROM users"})).json()
    assert read["status"] == "ok" and read["data"]["rows"] == [{"n": 2}]
    write = (await client.request("POST", f"/db-connections/{c['id']}/query", expect=200,
                                  json={"query": "DELETE FROM users WHERE id = 1"})).json()
    assert write["status"] == "approval_required"
    still = (await client.request("POST", f"/db-connections/{c['id']}/query", expect=200,
                                  json={"query": "SELECT count(*) AS n FROM users"})).json()
    assert still["data"]["rows"] == [{"n": 2}]  # nothing executed before approval
    approved = (await client.request("POST", f"/approvals/{write['approval_id']}/approve", expect=200, json={})).json()
    assert approved["execution"]["status"] == "ok"
    after = (await client.request("POST", f"/db-connections/{c['id']}/query", expect=200,
                                  json={"query": "SELECT count(*) AS n FROM users"})).json()
    assert after["data"]["rows"] == [{"n": 1}]


FIX_EDIT = json.dumps({
    "summary": "Invert the expiry comparison in verify_token so only expired tokens are rejected.",
    "edits": [{"path": "taskboard/auth.py", "action": "replace",
               "find": "    if current < expires_at:\n        raise AuthError(\"token expired\")",
               "replace": "    if current > expires_at:\n        raise AuthError(\"token expired\")"}],
    "notes": [],
})
DEBUG_REPORT = json.dumps({
    "summary": "verify_token rejects valid tokens: the expiry comparison is inverted.",
    "root_cause": "taskboard/auth.py line 53 uses `current < expires_at`, which raises for tokens that are still valid.",
    "evidence": [{"file": "taskboard/auth.py", "line": 53, "explanation": "inverted comparison"}],
    "affected_files": ["taskboard/auth.py"],
    "suggested_fix": "if current > expires_at: raise AuthError('token expired')",
    "confidence": "high",
})


@requires_sandbox
async def test_fix_bug_flow_with_approval(client, fixtures_root, fake, container):
    await fake.post("/_control/rules", json={"rules": [
        {"contains": "Apply proposed code changes", "reply": "{}"},
        {"contains": "How to propose edits", "reply": FIX_EDIT},
        {"contains": "debugging expert", "reply": DEBUG_REPORT},
    ]})
    await client.request("GET", "/models/health", params={"refresh": "true"}, expect=200)
    pid = await import_fixture(client, fixtures_root, name="Fix me")
    conv = (await client.request("POST", "/conversations", expect=201, json={"project_id": pid})).json()
    phase1 = await run_to_end(client, conv["id"], "Find the authentication bug and fix it.")
    assert phase1["run"]["status"] == "awaiting_approval", phase1["run"]
    verifications = [d for t, d in phase1["events"] if t == "verification"]
    proposed = [v for v in verifications if v.get("source") == "tests" and v.get("phase") == "proposed"]
    assert proposed and proposed[-1]["status"] == "passed"  # tests really ran in the sandbox with the change
    approval_id = next(d["approval_id"] for t, d in phase1["events"] if t == "approval_required")
    from pathlib import Path

    project_file = next(Path(container.settings.projects_root).rglob(f"{pid}")) / "taskboard" / "auth.py"
    assert "current < expires_at" in project_file.read_text()  # not modified before approval
    await client.request("POST", f"/approvals/{approval_id}/approve", expect=200, json={})
    run_id = phase1["run_id"]

    async def finished():
        run = (await client.request("GET", f"/runs/{run_id}")).json()
        return run if run["status"] in ("succeeded", "failed") else None

    run = await wait_for(finished, timeout=300)
    assert run["status"] == "succeeded", run
    assert "current > expires_at" in project_file.read_text()
    tests = run["result"]["tests"]
    assert any(t["data"]["phase"] == "applied" and t["status"] == "passed" for t in tests)
    messages = (await client.request("GET", f"/conversations/{conv['id']}/messages", expect=200)).json()
    assert "After applying changes" in messages[-1]["content"]
