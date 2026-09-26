"""Single-shot runtimes: structured JSON output, or streamed markdown text."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent, history_messages, task_prompt
from app.agents.json_output import compact_schema, extract_json_object
from app.agents.schemas import OUTPUT_SCHEMAS
from app.core.exceptions import AgentError, ErrorCode, ModelError
from app.core.logging import get_logger
from app.models.providers.base import ChatMessage, ChatRequest

log = get_logger(__name__)
M = TypeVar("M", bound=BaseModel)
_CITATION = re.compile(r"\[(S\d{1,3})\]")


def output_instructions(model: type[BaseModel]) -> str:
    skeleton = json.dumps(compact_schema(model.model_json_schema()), indent=1)
    return ("Respond with ONLY one JSON object (no prose before or after) with this structure:\n" + skeleton)


async def structured_call(agent: BaseAgent, ctx: AgentContext, task: AgentTask, messages: list[ChatMessage],
                          model: type[M], result: AgentResult, *, max_tokens: int | None = None,
                          **req_overrides: object) -> M:
    """Call the routed model for a JSON object matching ``model``; one repair attempt on invalid output."""
    schema = model.model_json_schema()
    attempt_messages = list(messages)
    last_error = ""
    budget = max_tokens or agent.spec.max_output_tokens
    for attempt in range(2):
        routed = await agent.call_model(ctx, attempt_messages, task, response_schema=schema, max_tokens=budget,
                                        **req_overrides)
        result.model_ids.append(routed.decision.model_id)
        result.prompt_tokens += routed.response.usage.prompt_tokens
        result.completion_tokens += routed.response.usage.completion_tokens
        result.notices.extend(n for n in routed.decision.notices if n not in result.notices)
        raw = routed.response.content
        truncated = routed.response.finish_reason == "length"
        data = extract_json_object(raw)
        if data is None:
            last_error = "The reply was not a JSON object."
        else:
            try:
                return model.model_validate(data)
            except ValidationError as exc:
                last_error = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:6])
        if truncated:  # a cut-off reply often parses to a nested fragment: report the real cause
            last_error = f"The reply was cut off at the output token limit ({budget} tokens)."
        log.warning("agent.invalid_structured_output", agent=agent.id, attempt=attempt + 1, error=last_error,
                    model_id=routed.decision.model_id, finish_reason=routed.response.finish_reason,
                    raw_preview=raw[:600])
        if attempt == 0 and truncated:
            # Truncated, not malformed: retry the same request with a larger budget instead of a repair prompt.
            budget = min((budget or 1024) * 2, 8192)
            attempt_messages = [*messages, ChatMessage(role="user", content="Keep every string field short. "
                                                       + output_instructions(model))]
        elif attempt == 0:
            attempt_messages = [*messages, ChatMessage(role="assistant", content=raw[:4000]),
                                ChatMessage(role="user", content=f"That output was invalid ({last_error}). "
                                            f"{output_instructions(model)}")]
    if truncated:
        raise AgentError(f"The {agent.spec.name}'s reply was cut off at the output token limit ({budget} tokens); "
                         "raise max_output_tokens for this agent or use a model that answers more concisely.",
                         code=ErrorCode.AGENT_INVALID_OUTPUT, detail=last_error)
    raise AgentError(f"The {agent.spec.name} returned output in an invalid format.",
                     code=ErrorCode.AGENT_INVALID_OUTPUT, detail=last_error)


class SingleShotAgent(BaseAgent):
    """One model call producing a validated structured output (spec.output_schema)."""

    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        if not self.spec.output_schema or self.spec.output_schema not in OUTPUT_SCHEMAS:
            raise AgentError(f"Agent '{self.id}' has no valid output_schema.", code=ErrorCode.CONFIGURATION_ERROR)
        schema_model = OUTPUT_SCHEMAS[self.spec.output_schema]
        await ctx.status(f"{self.spec.name}: analysing…")
        messages = [
            ChatMessage(role="system", content=self.system_prompt(output_instructions(schema_model))),
            *history_messages(task),
            ChatMessage(role="user", content=task_prompt(task)),
        ]
        output = await structured_call(self, ctx, task, messages, schema_model, result)
        data = output.model_dump()
        result.output = data
        result.summary = str(data.get("summary") or data.get("title") or data.get("answer") or "")[:1000]
        result.citations = sorted(set(data.get("citations") or []) | set(_CITATION.findall(json.dumps(data))))
        text_field = data.get("answer") or data.get("content")
        if isinstance(text_field, str):
            result.text = text_field


class StreamTextAgent(BaseAgent):
    """Streams a markdown answer token-by-token to the client; citations are inline [S#] markers."""

    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        extra = ""
        if task.context:
            extra = ("Cite the context blocks you rely on inline using their ids, e.g. [S1]. "
                     "Only cite blocks that actually support the statement.")
        messages = [
            ChatMessage(role="system", content=self.system_prompt(extra)),
            *history_messages(task),
            ChatMessage(role="user", content=task_prompt(task)),
        ]
        request = ChatRequest(messages=messages, max_tokens=self.spec.max_output_tokens, temperature=self.spec.temperature)
        decision, stream = await ctx.router.stream_chat(
            request, self.requirements(task, latency_sensitive=True),
            context={"run_id": str(ctx.run_id) if ctx.run_id else None, "agent_id": self.id},
        )
        ctx.used_models.add(decision.model_id)
        result.model_ids.append(decision.model_id)
        await ctx.emit("model_selected", agent=self.id, model_id=decision.model_id, role=str(decision.role),
                       reason=decision.reason[:300], notices=decision.notices)
        for notice in decision.notices:
            result.notices.append(notice)
            await ctx.emit("notice", level="warning", message=notice)
        parts: list[str] = []
        try:
            async for chunk in stream:
                if chunk.content:
                    parts.append(chunk.content)
                    await ctx.emit("token", agent=self.id, text=chunk.content)
                if chunk.usage:
                    result.prompt_tokens += chunk.usage.prompt_tokens
                    result.completion_tokens += chunk.usage.completion_tokens
        except ModelError:
            if parts:  # partial answer streamed: report the interruption explicitly
                await ctx.emit("notice", level="error", message="The model stream was interrupted; the answer is incomplete.")
            raise
        text = "".join(parts).strip()
        if not text:
            raise AgentError(f"The {self.spec.name} produced an empty answer.", code=ErrorCode.AGENT_EMPTY_RESPONSE)
        result.text = text
        result.summary = text[:300]
        result.citations = sorted(set(_CITATION.findall(text)), key=lambda c: int(c[1:]))
        result.output = {"answer": text, "citations": result.citations}
