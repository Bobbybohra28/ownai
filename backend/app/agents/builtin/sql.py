"""SQL agent: generates SQL from schema knowledge, validates it with a real parser, and
executes read-only queries on registered connections. Modifying statements are never
run without explicit user approval."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.agents.base import AgentContext, AgentResult, AgentTask, BaseAgent, history_messages, task_prompt
from app.agents.runtimes.single_shot import output_instructions, structured_call
from app.agents.schemas import SQLOutput
from app.database.models import DbConnection
from app.models.providers.base import ChatMessage
from app.tools.sql import classify_sql


class SQLAgent(BaseAgent):
    async def _connection(self, ctx: AgentContext, task: AgentTask) -> DbConnection | None:
        if ctx.project is None:
            return None
        wanted = task.inputs.get("connection_id")
        async with ctx.tool_services.sessions() as session:
            stmt = select(DbConnection).where(DbConnection.project_id == ctx.project.id,
                                              DbConnection.organization_id == ctx.org_id)
            if wanted:
                stmt = stmt.where(DbConnection.id == uuid.UUID(str(wanted)))
            return (await session.execute(stmt.order_by(DbConnection.created_at).limit(1))).scalar_one_or_none()

    async def execute(self, task: AgentTask, ctx: AgentContext, result: AgentResult) -> None:
        connection = await self._connection(ctx, task)
        schema_text = ""
        if connection is not None:
            await ctx.status(f"Reading schema of '{connection.name}'…")
            outcome = await self.use_tool(ctx, task, "inspect_database", {"connection_id": str(connection.id)}, result)
            if outcome.ok:
                tables = outcome.data.get("tables", {})
                schema_text = "Live database schema:\n" + "\n".join(
                    f"- {name}({', '.join(cols)})" for name, cols in list(tables.items())[:80])
        dialect = connection.dialect if connection else "postgresql"
        guidance = (f"Write {dialect} SQL. Use only tables/columns that appear in the schema or context; if they are "
                    "not known, say so in the explanation. Prefer read-only queries unless the user explicitly asks to "
                    "modify data.\n" + output_instructions(SQLOutput))
        messages = [
            ChatMessage(role="system", content=self.system_prompt(guidance)),
            *history_messages(task),
            ChatMessage(role="user", content=task_prompt(task, extra=schema_text)),
        ]
        await ctx.status("Generating SQL…")
        output = await structured_call(self, ctx, task, messages, SQLOutput, result)
        classification = classify_sql(output.sql, dialect)
        if classification.parse_error:
            # one repair round with the parser error
            messages += [ChatMessage(role="assistant", content=output.model_dump_json()),
                         ChatMessage(role="user", content=f"The SQL does not parse: {classification.parse_error}. "
                                     + output_instructions(SQLOutput))]
            output = await structured_call(self, ctx, task, messages, SQLOutput, result)
            classification = classify_sql(output.sql, dialect)
        data = {**output.model_dump(), "category": classification.category, "warnings": classification.warnings,
                "valid": classification.parse_error is None, "parse_error": classification.parse_error}
        text = f"```sql\n{output.sql.strip()}\n```\n\n{output.explanation}".strip()
        if classification.parse_error:
            result.notices.append(f"The generated SQL is not valid {dialect}: {classification.parse_error}")
        elif connection is not None:
            await ctx.status("Running query…" if classification.is_read_only else "Requesting approval for SQL…")
            outcome = await self.use_tool(ctx, task, "run_sql", {"connection_id": str(connection.id),
                                                                  "query": output.sql, "max_rows": 50}, result)
            data["execution"] = {"status": outcome.status, "summary": outcome.summary,
                                 "columns": outcome.data.get("columns"), "rows": (outcome.data.get("rows") or [])[:50]}
            if outcome.status == "approval_required":
                result.status = "needs_approval"
                text += f"\n\n**This statement modifies the database ({classification.category}) and is waiting for your approval.**"
            elif outcome.ok and outcome.data.get("rows") is not None:
                text += f"\n\nResult: {outcome.summary}"
            else:
                text += f"\n\nExecution: {outcome.summary}"
        else:
            result.notices.append("No database connection is registered for this project; the SQL was validated but not executed.")
        result.output = data
        result.text = text
        result.summary = output.explanation[:300] or f"{classification.category} SQL generated"
