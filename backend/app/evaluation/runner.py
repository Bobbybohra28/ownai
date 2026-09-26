"""Evaluation runner: executes benchmark datasets against a specific model and records metrics.

Scoring is objective: generated code is executed against assertions in the sandbox,
SQL is executed against a real (in-memory, locked-down) SQLite database, tool calls are
compared with expected tools/arguments, retrieval is checked against expected files.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import update

from app.agents.json_output import extract_json_object
from app.core.config import REPO_ROOT
from app.core.exceptions import AppError, ModelError, NotFoundError
from app.core.logging import get_logger
from app.database.base import utcnow
from app.database.models import Evaluation, EvaluationResult
from app.models.providers.base import ChatMessage, ChatRequest, ToolSchema

if TYPE_CHECKING:
    from app.core.dependencies import Container

log = get_logger(__name__)

DATASET_DIR = REPO_ROOT / "evaluation" / "datasets"
CATEGORIES = {"coding", "debugging", "rag", "sql", "tool_calling", "reasoning"}
_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n([\s\S]*?)```")


class Dataset(BaseModel):
    name: str
    category: str
    description: str = ""
    cases: list[dict[str, Any]]
    schema_sql: str | None = Field(default=None, alias="schema")
    tools: list[dict[str, Any]] = Field(default_factory=list)


def list_datasets(directory: Path = DATASET_DIR) -> list[Dataset]:
    datasets = []
    for path in sorted(directory.glob("*.json")):
        datasets.append(Dataset.model_validate(json.loads(path.read_text(encoding="utf-8"))))
    return datasets


def get_dataset(name: str) -> Dataset:
    for dataset in list_datasets():
        if dataset.name == name:
            return dataset
    raise NotFoundError(f"Unknown evaluation dataset '{name}'.")


def extract_code(text: str) -> str:
    blocks = _CODE_BLOCK.findall(text)
    return max(blocks, key=len).strip() if blocks else text.strip()


class CaseResult(BaseModel):
    case_id: str
    passed: bool
    score: float = 0.0
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error_code: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class EvaluationRunner:
    def __init__(self, container: Container) -> None:
        self.c = container

    async def _chat(self, model_id: str, messages: list[ChatMessage], **kwargs: Any) -> tuple[str, Any, int]:
        provider = self.c.providers[model_id]
        started = time.perf_counter()
        response = await provider.chat(ChatRequest(messages=messages, temperature=0.0, **kwargs))
        return response.content, response, int((time.perf_counter() - started) * 1000)

    async def _sandbox_python(self, code: str) -> tuple[bool, str]:
        result = await self.c.sandbox.execute(profile="python", argv=["python", ".ownai_eval.py"],
                                              overlay={".ownai_eval.py": code}, timeout_s=30, project_label="evaluation")
        return result.ok, (result.stdout + result.stderr)[-1500:]

    async def case_coding(self, model_id: str, case: dict[str, Any]) -> CaseResult:
        content, resp, ms = await self._chat(model_id, [
            ChatMessage(role="system", content="You are an expert Python programmer. Reply with a single ```python code block."),
            ChatMessage(role="user", content=case["prompt"]),
        ], max_tokens=800)
        code = extract_code(content)
        ok, output = await self._sandbox_python(code + "\n\n" + case["tests"] + "\nprint('ALL_TESTS_PASSED')\n")
        passed = ok and "ALL_TESTS_PASSED" in output
        return CaseResult(case_id=case["id"], passed=passed, score=1.0 if passed else 0.0, latency_ms=ms,
                          prompt_tokens=resp.usage.prompt_tokens, completion_tokens=resp.usage.completion_tokens,
                          error_code=None if passed else "VERIFICATION_FAILED", detail={"output": output[-600:]})

    async def case_debugging(self, model_id: str, case: dict[str, Any]) -> CaseResult:
        content, resp, ms = await self._chat(model_id, [
            ChatMessage(role="system", content="Fix the bug. Reply with the complete corrected code in one ```python block."),
            ChatMessage(role="user", content=f"Code:\n```python\n{case['code']}```\nProblem: {case['error']}"),
        ], max_tokens=800)
        code = extract_code(content)
        ok, output = await self._sandbox_python(code + "\n\n" + case["tests"] + "\nprint('ALL_TESTS_PASSED')\n")
        passed = ok and "ALL_TESTS_PASSED" in output
        return CaseResult(case_id=case["id"], passed=passed, score=1.0 if passed else 0.0, latency_ms=ms,
                          prompt_tokens=resp.usage.prompt_tokens, completion_tokens=resp.usage.completion_tokens,
                          error_code=None if passed else "VERIFICATION_FAILED", detail={"output": output[-600:]})

    @staticmethod
    def _run_sqlite(schema: str, query: str) -> list[list[Any]]:
        db = sqlite3.connect(":memory:")
        try:
            db.executescript(schema)

            def authorizer(action: int, *_: Any) -> int:
                allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                           getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
                return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

            db.set_authorizer(authorizer)  # generated SQL may only read
            rows = db.execute(query).fetchmany(1000)
            return [[round(v, 4) if isinstance(v, float) else v for v in r] for r in rows]
        finally:
            db.close()

    async def case_sql(self, model_id: str, case: dict[str, Any], schema: str) -> CaseResult:
        content, resp, ms = await self._chat(model_id, [
            ChatMessage(role="system", content="Write one SQLite query answering the question. Reply with JSON {\"sql\": \"...\"}."),
            ChatMessage(role="user", content=f"Schema:\n{schema}\n\nQuestion: {case['question']}"),
        ], max_tokens=300, json_mode=True)
        data = extract_json_object(content) or {}
        sql = str(data.get("sql") or extract_code(content))
        try:
            rows = self._run_sqlite(schema, sql)
            expected = [[round(v, 4) if isinstance(v, float) else v for v in r] for r in case["expected"]]
            passed = rows == expected if case.get("ordered") else sorted(map(repr, rows)) == sorted(map(repr, expected))
            detail: dict[str, Any] = {"sql": sql, "rows": rows[:20]}
            error = None if passed else "WRONG_RESULT"
        except sqlite3.Error as exc:
            passed, detail, error = False, {"sql": sql, "error": str(exc)}, "SQL_ERROR"
        return CaseResult(case_id=case["id"], passed=passed, score=1.0 if passed else 0.0, latency_ms=ms,
                          prompt_tokens=resp.usage.prompt_tokens, completion_tokens=resp.usage.completion_tokens,
                          error_code=error, detail=detail)

    async def case_tool_calling(self, model_id: str, case: dict[str, Any], tools: list[dict[str, Any]]) -> CaseResult:
        config = self.c.registry.get(model_id)
        native = bool(config and config.capabilities.supports_tools)
        schemas = [ToolSchema(**t) for t in tools]
        if native:
            content, resp, ms = await self._chat(model_id, [ChatMessage(role="user", content=case["instruction"])],
                                                 tools=schemas, max_tokens=200)
            call = resp.tool_calls[0] if resp.tool_calls else None
            name, args = (call.name, call.arguments) if call else (None, {})
        else:
            catalog = "\n".join(f"- {t['name']}: {t['description']} args={json.dumps(t['parameters']['properties'])}" for t in tools)
            content, resp, ms = await self._chat(model_id, [
                ChatMessage(role="system", content=f"Tools:\n{catalog}\nReply with JSON {{\"tool\": name, \"args\": {{...}}}}."),
                ChatMessage(role="user", content=case["instruction"]),
            ], max_tokens=200, json_mode=True)
            data = extract_json_object(content) or {}
            name, args = data.get("tool"), data.get("args") or {}
        expected_args = case.get("expected_args", {})
        args_ok = all(str(args.get(k)).strip("/") == str(v).strip("/") if not isinstance(v, list) else args.get(k) == v
                      for k, v in expected_args.items())
        passed = name == case["expected_tool"] and args_ok
        return CaseResult(case_id=case["id"], passed=passed, score=1.0 if passed else (0.5 if name == case["expected_tool"] else 0.0),
                          latency_ms=ms, prompt_tokens=resp.usage.prompt_tokens, completion_tokens=resp.usage.completion_tokens,
                          error_code=None if passed else ("TOOL_ARGS_MISMATCH" if name == case["expected_tool"] else "WRONG_TOOL"),
                          detail={"tool": name, "args": args, "native": native})

    async def case_reasoning(self, model_id: str, case: dict[str, Any]) -> CaseResult:
        content, resp, ms = await self._chat(model_id, [ChatMessage(role="user", content=case["question"])], max_tokens=200)
        normalized = content.strip().lower().replace(" ", "")
        accepted = [a.lower().replace(" ", "") for a in case.get("accept", [case["answer"]])]
        passed = any(a in normalized for a in accepted)
        return CaseResult(case_id=case["id"], passed=passed, score=1.0 if passed else 0.0, latency_ms=ms,
                          prompt_tokens=resp.usage.prompt_tokens, completion_tokens=resp.usage.completion_tokens,
                          error_code=None if passed else "WRONG_ANSWER", detail={"answer": content[:300]})

    async def case_rag(self, model_id: str, case: dict[str, Any], org_id: uuid.UUID, project_id: uuid.UUID) -> CaseResult:
        started = time.perf_counter()
        retrieval = await self.c.retriever.retrieve(case["question"], org_id=org_id, project_id=project_id, top_k=5)
        files = [c.file_path for c in retrieval.chunks]
        hit = any(f in files for f in case["expected_files"])
        context = "\n\n".join(f"[{c.file_path}]\n{c.content[:1500]}" for c in retrieval.chunks)
        content, resp, _ = await self._chat(model_id, [
            ChatMessage(role="system", content="Answer using only the context."),
            ChatMessage(role="user", content=f"Context:\n{context}\n\nQuestion: {case['question']}"),
        ], max_tokens=300)
        keywords = case.get("keywords", [])
        kw_hits = sum(1 for k in keywords if k.lower() in content.lower())
        score = (0.6 if hit else 0.0) + (0.4 * kw_hits / len(keywords) if keywords else 0.4)
        return CaseResult(case_id=case["id"], passed=hit and kw_hits == len(keywords), score=round(score, 3),
                          latency_ms=int((time.perf_counter() - started) * 1000), prompt_tokens=resp.usage.prompt_tokens,
                          completion_tokens=resp.usage.completion_tokens, error_code=None if hit else "RETRIEVAL_MISS",
                          detail={"retrieved": files, "semantic": retrieval.used_dense, "keywords_found": kw_hits})

    async def run(self, evaluation_id: uuid.UUID) -> None:
        async with self.c.sessions() as session:
            evaluation = await session.get(Evaluation, evaluation_id)
            if evaluation is None:
                raise NotFoundError("Evaluation not found.")
            evaluation.status = "running"
            await session.commit()
            dataset_name, model_id, params = evaluation.dataset, evaluation.model_id, dict(evaluation.metrics or {})
        dataset = get_dataset(dataset_name)
        results: list[CaseResult] = []
        for case in dataset.cases:
            try:
                if dataset.category == "coding":
                    result = await self.case_coding(model_id, case)
                elif dataset.category == "debugging":
                    result = await self.case_debugging(model_id, case)
                elif dataset.category == "sql":
                    result = await self.case_sql(model_id, case, dataset.schema_sql or "")
                elif dataset.category == "tool_calling":
                    result = await self.case_tool_calling(model_id, case, dataset.tools)
                elif dataset.category == "reasoning":
                    result = await self.case_reasoning(model_id, case)
                elif dataset.category == "rag":
                    result = await self.case_rag(model_id, case, uuid.UUID(params["org_id"]), uuid.UUID(params["project_id"]))
                else:
                    raise AppError(f"Unsupported category {dataset.category}")
            except ModelError as exc:
                result = CaseResult(case_id=case["id"], passed=False, error_code=str(exc.code), detail={"error": exc.message})
            except AppError as exc:
                result = CaseResult(case_id=case["id"], passed=False, error_code=str(exc.code), detail={"error": exc.message})
            results.append(result)
            async with self.c.sessions() as session:
                session.add(EvaluationResult(evaluation_id=evaluation_id, **result.model_dump(exclude={"case_id"}),
                                             case_id=result.case_id))
                await session.commit()
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        model_errors = sum(1 for r in results if r.error_code and r.error_code.startswith("MODEL_"))
        latencies = sorted(r.latency_ms for r in results if r.latency_ms)
        completion = sum(r.completion_tokens for r in results)
        metrics = {
            **{k: v for k, v in params.items() if k in ("org_id", "project_id")},
            "accuracy": round(passed / total, 4) if total else 0.0,
            "mean_score": round(sum(r.score for r in results) / total, 4) if total else 0.0,
            "success_rate": round((total - model_errors) / total, 4) if total else 0.0,
            "latency_ms_p50": latencies[len(latencies) // 2] if latencies else None,
            "latency_ms_mean": int(sum(latencies) / len(latencies)) if latencies else None,
            "tokens_per_second": round(completion / (sum(latencies) / 1000), 2) if latencies and sum(latencies) else None,
            "prompt_tokens": sum(r.prompt_tokens for r in results), "completion_tokens": completion,
            "tool_errors": sum(1 for r in results if r.error_code in ("WRONG_TOOL", "TOOL_ARGS_MISMATCH")),
            "verification_failures": sum(1 for r in results if r.error_code == "VERIFICATION_FAILED"),
            "model_errors": model_errors,
        }
        async with self.c.sessions() as session:
            await session.execute(update(Evaluation).where(Evaluation.id == evaluation_id).values(
                status="completed", total=total, passed=passed, metrics=metrics, finished_at=utcnow()))
            await session.commit()
        log.info("evaluation.completed", evaluation_id=str(evaluation_id), model_id=model_id, dataset=dataset_name,
                 accuracy=metrics["accuracy"])


async def run_evaluation_job(container: Container, evaluation_id: uuid.UUID) -> None:
    try:
        await EvaluationRunner(container).run(evaluation_id)
    except Exception as exc:
        log.exception("evaluation.failed", evaluation_id=str(evaluation_id))
        async with container.sessions() as session:
            await session.execute(update(Evaluation).where(Evaluation.id == evaluation_id).values(
                status="failed", error=getattr(exc, "message", None) or type(exc).__name__, finished_at=utcnow()))
            await session.commit()
