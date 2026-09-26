"""structured_call: truncated replies are retried with a larger budget and reported as truncation."""

from typing import Any

import pytest
from pydantic import BaseModel

from app.agents.base import AgentResult, AgentTask, BaseAgent
from app.agents.runtimes.single_shot import structured_call
from app.agents.spec import AgentSpec
from app.core.exceptions import AgentError
from app.models.providers.base import ChatMessage, ChatResponse
from app.models.router import ModelRole, RoutedChatResult, RoutingDecision


class Diagnosis(BaseModel):
    root_cause: str
    summary: str


class ScriptedAgent(BaseAgent):
    def __init__(self, replies: list[tuple[str, str]]) -> None:
        super().__init__(AgentSpec(id="debugging", name="Debugging Agent", description="d", category="development",
                                   runtime="single_shot", max_output_tokens=900))
        self.replies = list(replies)
        self.budgets: list[int | None] = []

    async def execute(self, task: Any, ctx: Any, result: Any) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def call_model(self, ctx: Any, messages: list[ChatMessage], task: AgentTask, *,
                         response_schema: Any = None, max_tokens: int | None = None, **_: Any) -> RoutedChatResult:
        self.budgets.append(max_tokens)
        content, finish = self.replies.pop(0)
        return RoutedChatResult(
            response=ChatResponse(model_id="m", model_name="m", content=content, finish_reason=finish),
            decision=RoutingDecision(model_id="m", model_name="m", role=ModelRole.CODING, reason="test"))


TRUNCATED = '{"root_cause": "verify_token rejects fresh tokens", "evidence": [{"file": "a.py", "line": 5}], "sugg'
TASK = AgentTask(step_id="s1", goal="g", request="r")


async def test_truncated_reply_retried_with_larger_budget() -> None:
    agent = ScriptedAgent([(TRUNCATED, "length"), ('{"root_cause": "x", "summary": "y"}', "stop")])
    out = await structured_call(agent, None, TASK, [], Diagnosis, AgentResult(agent_id="debugging", status="succeeded"))  # type: ignore[arg-type]
    assert out.root_cause == "x"
    assert agent.budgets == [900, 1800]


async def test_truncation_reported_as_truncation() -> None:
    agent = ScriptedAgent([(TRUNCATED, "length"), (TRUNCATED, "length")])
    with pytest.raises(AgentError) as exc:
        await structured_call(agent, None, TASK, [], Diagnosis, AgentResult(agent_id="debugging", status="succeeded"))  # type: ignore[arg-type]
    assert "cut off at the output token limit (1800 tokens)" in str(exc.value)
    assert "Field required" not in (exc.value.detail or "")


async def test_complete_reply_despite_length_flag_is_accepted() -> None:
    agent = ScriptedAgent([('{"root_cause": "x", "summary": "y"}', "length")])
    out = await structured_call(agent, None, TASK, [], Diagnosis, AgentResult(agent_id="debugging", status="succeeded"))  # type: ignore[arg-type]
    assert out.summary == "y"
