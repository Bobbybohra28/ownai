"""Tool-using agent loop.

Two protocols, chosen per model capability:
* native OpenAI-style tool calls when the routed model supports them;
* a JSON action protocol ({"action": "call_tool" | "final", ...}) with constrained
  decoding for models without native tool calling.
Every tool call goes through the ToolExecutor (validation, permissions, approvals, audit).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent, history_messages, task_prompt
from app.agents.json_output import compact_schema, extract_json_object
from app.agents.runtimes.single_shot import output_instructions, structured_call
from app.agents.schemas import OUTPUT_SCHEMAS, AnalysisReport
from app.core.exceptions import AgentError, ErrorCode
from app.models.providers.base import ChatMessage, ChatRequest

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["call_tool", "final"]},
        "tool": {"type": "string"},
        "args": {"type": "object"},
        "output": {"type": "object"},
    },
    "required": ["action"],
}


class ToolLoopAgent(BaseAgent):
    def _output_model(self) -> type[BaseModel]:
        return OUTPUT_SCHEMAS.get(self.spec.output_schema or "", AnalysisReport)

    def _tool_catalog(self, ctx: AgentContext) -> str:
        lines = []
        for schema in ctx.tool_registry.schemas(self.spec.allowed_tools):
            args = json.dumps(compact_schema(schema.parameters))
            lines.append(f"- {schema.name}: {schema.description}\n  args: {args}")
        return "\n".join(lines)

    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        output_model = self._output_model()
        requirements = self.requirements(task, needs_tools=True)
        decision = await ctx.router.select(requirements)
        config = ctx.router.registry.get(decision.model_id)
        native = bool(config and config.capabilities.supports_tools)
        await ctx.status(f"{self.spec.name}: working…")
        if native:
            await self._native_loop(task, ctx, result, output_model)
        else:
            await self._json_loop(task, ctx, result, output_model)

    # ------------------------------------------------------------------------------------------
    async def _run_tool(self, ctx: AgentContext, task: AgentTask, result: AgentResult, name: str,
                        args: dict[str, Any]) -> str:
        outcome = await self.use_tool(ctx, task, name, args if isinstance(args, dict) else {}, result)
        return outcome.for_model()

    def _finish(self, result: AgentResult, output: BaseModel) -> None:
        data = output.model_dump()
        result.output = data
        result.summary = str(data.get("summary") or data.get("answer") or data.get("title") or "")[:1000]
        text = data.get("answer") or data.get("content")
        if isinstance(text, str):
            result.text = text
        result.citations = list(data.get("citations") or [])

    async def _json_loop(self, task: AgentTask, ctx: AgentContext, result: AgentResult,
                         output_model: type[BaseModel]) -> None:
        protocol = (
            "You can use these tools:\n" + self._tool_catalog(ctx) + "\n\n"
            "Each reply must be ONE JSON object, either\n"
            '  {"action": "call_tool", "tool": "<tool name>", "args": {...}}\n'
            'or, when you have enough information,\n  {"action": "final", "output": <result>}\n'
            "where <result> has this structure:\n" + json.dumps(compact_schema(output_model.model_json_schema()), indent=1)
            + "\nCall one tool per reply. Use at most a few tool calls; prefer the provided context when it suffices."
        )
        messages = [
            ChatMessage(role="system", content=self.system_prompt(protocol)),
            *history_messages(task),
            ChatMessage(role="user", content=task_prompt(task)),
        ]
        invalid = 0
        for _ in range(self.spec.max_iterations):
            if ctx.is_cancelled and await ctx.is_cancelled():
                raise AgentError("The run was cancelled.", code=ErrorCode.RUN_CANCELLED)
            routed = await self.call_model(ctx, messages, task, response_schema=ACTION_SCHEMA, needs_tools=True)
            result.model_ids.append(routed.decision.model_id)
            result.prompt_tokens += routed.response.usage.prompt_tokens
            result.completion_tokens += routed.response.usage.completion_tokens
            raw = routed.response.content
            action = extract_json_object(raw)
            messages.append(ChatMessage(role="assistant", content=raw[:6000]))
            if not action or action.get("action") not in ("call_tool", "final"):
                invalid += 1
                if invalid > 1:
                    break
                messages.append(ChatMessage(role="user", content="Invalid reply. Reply with one JSON object as specified."))
                continue
            if action["action"] == "final":
                payload = action.get("output")
                if not isinstance(payload, dict):
                    payload = {k: v for k, v in action.items() if k != "action"}
                try:
                    self._finish(result, output_model.model_validate(payload))
                    return
                except ValidationError as exc:
                    invalid += 1
                    if invalid > 1:
                        break
                    messages.append(ChatMessage(role="user", content=f"The final output is invalid: "
                                                f"{exc.errors()[0]['msg']}. {output_instructions(output_model)}"))
                    continue
            tool_name = str(action.get("tool") or "")
            tool_text = await self._run_tool(ctx, task, result, tool_name, action.get("args") or {})
            if result.approval_ids:
                result.status = "needs_approval"
                result.summary = f"Waiting for approval of {tool_name}."
                return
            messages.append(ChatMessage(role="user", content=f"<tool_result name=\"{tool_name}\">\n{tool_text}\n</tool_result>"))
        # iteration budget exhausted: ask for a final answer without tools
        messages.append(ChatMessage(role="user", content="Stop using tools now. " + output_instructions(output_model)))
        output = await structured_call(self, ctx, task, messages, output_model, result)
        self._finish(result, output)

    async def _native_loop(self, task: AgentTask, ctx: AgentContext, result: AgentResult,
                           output_model: type[BaseModel]) -> None:
        tools = ctx.tool_registry.schemas(self.spec.allowed_tools)
        messages = [
            ChatMessage(role="system", content=self.system_prompt(
                "Use the available tools when needed. When done, reply with the final result. "
                + output_instructions(output_model))),
            *history_messages(task),
            ChatMessage(role="user", content=task_prompt(task)),
        ]
        for _ in range(self.spec.max_iterations):
            if ctx.is_cancelled and await ctx.is_cancelled():
                raise AgentError("The run was cancelled.", code=ErrorCode.RUN_CANCELLED)
            routed = await ctx.router.chat(
                ChatRequest(messages=messages, tools=tools, max_tokens=self.spec.max_output_tokens,
                            temperature=self.spec.temperature),
                self.requirements(task, needs_tools=True),
                context={"run_id": str(ctx.run_id) if ctx.run_id else None, "agent_id": self.id},
            )
            ctx.used_models.add(routed.decision.model_id)
            result.model_ids.append(routed.decision.model_id)
            result.prompt_tokens += routed.response.usage.prompt_tokens
            result.completion_tokens += routed.response.usage.completion_tokens
            response = routed.response
            if not response.tool_calls:
                data = extract_json_object(response.content)
                if data is not None:
                    try:
                        self._finish(result, output_model.model_validate(data))
                        return
                    except ValidationError:
                        pass
                messages.append(ChatMessage(role="assistant", content=response.content[:6000]))
                messages.append(ChatMessage(role="user", content=output_instructions(output_model)))
                continue
            messages.append(ChatMessage(role="assistant", content=response.content or "", tool_calls=response.tool_calls))
            for call in response.tool_calls:
                text = await self._run_tool(ctx, task, result, call.name, call.arguments)
                messages.append(ChatMessage(role="tool", content=text, tool_call_id=call.id, name=call.name))
                if result.approval_ids:
                    result.status = "needs_approval"
                    result.summary = f"Waiting for approval of {call.name}."
                    return
        raise AgentError(f"The {self.spec.name} did not finish within {self.spec.max_iterations} steps.",
                         code=ErrorCode.AGENT_FAILED)

