"""SQL tools: statement classification, safe read-only execution, schema inspection.

* Every statement is parsed with sqlglot and classified (read / write / ddl / admin).
* Reads run inside a READ ONLY transaction with a statement timeout and a row cap.
* Writes, DDL and admin statements (DROP, DELETE, TRUNCATE, ALTER, UPDATE, …) require
  explicit human approval before they execute.
* Credentials are decrypted only at connection time and never shown to models.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import sqlglot
from pydantic import Field
from sqlglot import exp

from app.core.exceptions import ErrorCode, NotFoundError, ToolError
from app.database.models import DbConnection
from app.security.secrets import mask_secrets
from app.tools.base import ApprovalRequest, PermissionLevel, Tool, ToolArgs, ToolContext, ToolOutcome

Category = Literal["read", "write", "ddl", "admin", "unknown"]
_ORDER = {"read": 0, "write": 1, "ddl": 2, "admin": 3, "unknown": 4}
_DIALECTS = {"postgresql": "postgres", "sqlite": "sqlite"}


@dataclass
class SQLClassification:
    category: Category
    statements: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def is_read_only(self) -> bool:
        return self.category == "read"


def _statement_category(stmt: exp.Expression) -> tuple[Category, list[str]]:
    warnings: list[str] = []
    if isinstance(stmt, exp.Select | exp.Union | exp.Intersect | exp.Except):
        if stmt.find(exp.Into):
            return "write", ["SELECT … INTO creates a table"]
        return "read", warnings
    if isinstance(stmt, exp.Describe | exp.Show):
        return "read", warnings
    if isinstance(stmt, exp.Update | exp.Delete):
        if not stmt.args.get("where"):
            warnings.append(f"{stmt.key.upper()} without WHERE affects every row")
        return "write", warnings
    if isinstance(stmt, exp.Insert | exp.Merge):
        return "write", warnings
    if isinstance(stmt, exp.Create | exp.Drop | exp.Alter | exp.TruncateTable):
        if isinstance(stmt, exp.Drop | exp.TruncateTable):
            warnings.append(f"{stmt.key.upper()} permanently removes data")
        return "ddl", warnings
    if isinstance(stmt, exp.Command):
        name = (stmt.this or "").upper() if isinstance(stmt.this, str) else ""
        if name in {"EXPLAIN", "SHOW", "DESCRIBE"}:
            text = stmt.sql().upper()
            return ("admin" if "ANALYZE" in text else "read"), warnings
        return "admin", [f"{name or 'Command'} statement"]
    if isinstance(stmt, exp.Grant | exp.Revoke | exp.Set | exp.Use | exp.Copy | exp.Transaction | exp.Commit | exp.Rollback):
        return "admin", warnings
    return "unknown", [f"Unrecognised statement type {stmt.key}"]


def classify_sql(sql: str, dialect: str = "postgresql") -> SQLClassification:
    try:
        parsed = [s for s in sqlglot.parse(sql, read=_DIALECTS.get(dialect, dialect)) if s is not None]
    except sqlglot.errors.ParseError as exc:
        return SQLClassification("unknown", parse_error=str(exc).splitlines()[0][:300])
    if not parsed:
        return SQLClassification("unknown", parse_error="No SQL statement found.")
    worst: Category = "read"
    warnings: list[str] = []
    for stmt in parsed:
        category, w = _statement_category(stmt)
        warnings += w
        if _ORDER[category] > _ORDER[worst]:
            worst = category
    return SQLClassification(worst, [s.sql(dialect=_DIALECTS.get(dialect, dialect)) for s in parsed], warnings)


# ------------------------------------------------------------------------------------------------
# Connections
# ------------------------------------------------------------------------------------------------

async def load_connection(ctx: ToolContext, connection_id: uuid.UUID) -> tuple[DbConnection, str | None]:
    project = ctx.require_project()
    async with ctx.services.sessions() as session:
        conn = await session.get(DbConnection, connection_id)
        if conn is None or conn.organization_id != ctx.org_id or conn.project_id != project.id:
            raise NotFoundError("Database connection not found.")
        session.expunge(conn)
    password = None
    if conn.secret_ciphertext:
        if ctx.services.secret_box is None:
            raise ToolError("Credential decryption is not configured.", code=ErrorCode.CONFIGURATION_ERROR)
        password = ctx.services.secret_box.decrypt(conn.secret_ciphertext)
    return conn, password


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


async def execute_sql(ctx: ToolContext, conn: DbConnection, password: str | None, sql: str, *, read_only: bool,
                      max_rows: int, timeout_s: int) -> dict[str, Any]:
    if conn.dialect == "postgresql":
        import asyncpg

        try:
            connection = await asyncpg.connect(host=conn.host, port=conn.port or 5432, user=conn.username,
                                               password=password, database=conn.database, timeout=10)
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            raise ToolError(f"Could not connect to database '{conn.name}': {type(exc).__name__}",
                            code=ErrorCode.DATABASE_ERROR, detail=str(exc)) from exc
        try:
            async with connection.transaction(readonly=read_only):
                await connection.execute(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}")
                if read_only:
                    rows: list[dict[str, Any]] = []
                    columns: list[str] = []
                    truncated = False
                    async for record in connection.cursor(sql):
                        if not columns:
                            columns = list(record.keys())
                        if len(rows) >= max_rows:
                            truncated = True
                            break
                        rows.append({k: _jsonable(v) for k, v in record.items()})
                    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
                status = await connection.execute(sql)
                return {"status": status}
        except asyncpg.PostgresError as exc:
            raise ToolError(f"The database returned an error: {mask_secrets(str(exc))[:400]}",
                            code=ErrorCode.DATABASE_ERROR) from exc
        finally:
            await connection.close()
    if conn.dialect == "sqlite":
        path = ctx.require_project().jail.resolve(conn.database, must_exist=True)

        def _run() -> dict[str, Any]:
            uri = f"file:{path}?mode={'ro' if read_only else 'rw'}"
            db = sqlite3.connect(uri, uri=True, timeout=timeout_s)
            try:
                cur = db.execute(sql)
                if read_only:
                    columns = [d[0] for d in cur.description or []]
                    fetched = cur.fetchmany(max_rows + 1)
                    rows = [{c: _jsonable(v) for c, v in zip(columns, r, strict=False)} for r in fetched[:max_rows]]
                    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": len(fetched) > max_rows}
                db.commit()
                return {"status": f"{cur.rowcount} row(s) affected"}
            finally:
                db.close()

        try:
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s + 5)
        except sqlite3.Error as exc:
            raise ToolError(f"The database returned an error: {exc}", code=ErrorCode.DATABASE_ERROR) from exc
    raise ToolError(f"Unsupported database dialect '{conn.dialect}'.", code=ErrorCode.DATABASE_ERROR)


async def inspect_schema(ctx: ToolContext, conn: DbConnection, password: str | None) -> dict[str, Any]:
    if conn.dialect == "postgresql":
        sql = ("SELECT table_schema, table_name, column_name, data_type, is_nullable FROM information_schema.columns "
               "WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1,2,ordinal_position")
        result = await execute_sql(ctx, conn, password, sql, read_only=True, max_rows=5000, timeout_s=20)
        tables: dict[str, list[str]] = {}
        for r in result["rows"]:
            name = f"{r['table_schema']}.{r['table_name']}"
            tables.setdefault(name, []).append(f"{r['column_name']} {r['data_type']}"
                                               f"{'' if r['is_nullable'] == 'YES' else ' NOT NULL'}")
        return {"tables": tables}
    if conn.dialect == "sqlite":
        result = await execute_sql(ctx, conn, password, "SELECT name FROM sqlite_master WHERE type='table' "
                                   "AND name NOT LIKE 'sqlite_%' ORDER BY name", read_only=True, max_rows=1000, timeout_s=20)
        tables = {}
        for r in result["rows"]:
            name = str(r["name"]).replace('"', '""')
            cols = await execute_sql(ctx, conn, password, f'PRAGMA table_info("{name}")', read_only=True,
                                     max_rows=500, timeout_s=10)
            tables[r["name"]] = [f"{c['name']} {c['type']}{' NOT NULL' if c['notnull'] else ''}"
                                 f"{' PRIMARY KEY' if c['pk'] else ''}" for c in cols["rows"]]
        return {"tables": tables}
    raise ToolError(f"Unsupported database dialect '{conn.dialect}'.", code=ErrorCode.DATABASE_ERROR)


# ------------------------------------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------------------------------------

class RunSQLArgs(ToolArgs):
    connection_id: uuid.UUID
    query: str = Field(min_length=1, max_length=20_000)
    max_rows: int = Field(default=200, ge=1, le=5000)


class RunSQLTool(Tool):
    name = "run_sql"
    description = ("Execute SQL on a registered project database. SELECT queries run read-only; any statement that "
                   "modifies data or schema (INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE…) requires user approval.")
    args_model = RunSQLArgs
    permission = PermissionLevel.READ  # escalated per statement via approval_needed
    timeout_s = 120

    def approval_needed(self, args: RunSQLArgs, ctx: ToolContext) -> ApprovalRequest | None:
        classification = classify_sql(args.query)
        if classification.is_read_only:
            return None
        risk = "critical" if classification.category in ("ddl", "admin") or classification.warnings else "high"
        return ApprovalRequest(
            title=f"Execute {classification.category.upper()} SQL",
            description=("; ".join(classification.warnings) + "\n" if classification.warnings else "") + args.query[:2000],
            risk_level=risk,  # type: ignore[arg-type]
        )

    async def run(self, args: RunSQLArgs, ctx: ToolContext) -> ToolOutcome:
        conn, password = await load_connection(ctx, args.connection_id)
        classification = classify_sql(args.query, conn.dialect)
        if classification.parse_error and not ctx.approved:
            return ToolOutcome(status="error", error_code="SQL_PARSE_ERROR",
                               summary=f"The SQL could not be parsed: {classification.parse_error}")
        read_only = classification.is_read_only
        if not read_only and not ctx.approved:
            # defense in depth: the executor routes these to approval before we get here
            raise ToolError("Modifying SQL requires approval.", code=ErrorCode.APPROVAL_REQUIRED)
        if not read_only and conn.read_only:
            return ToolOutcome(status="denied", error_code="SQL_NOT_ALLOWED",
                               summary=f"Connection '{conn.name}' is registered as read-only; write statements are blocked.")
        result = await execute_sql(ctx, conn, password, args.query, read_only=read_only, max_rows=args.max_rows,
                                   timeout_s=60)
        summary = (f"{result['row_count']} row(s){' (truncated)' if result.get('truncated') else ''}" if read_only
                   else f"Executed: {result.get('status')}")
        return ToolOutcome(status="ok", summary=summary,
                           data={**result, "category": classification.category, "warnings": classification.warnings})


class InspectDatabaseArgs(ToolArgs):
    connection_id: uuid.UUID


class InspectDatabaseTool(Tool):
    name = "inspect_database"
    description = "List tables and columns of a registered project database (read-only)."
    args_model = InspectDatabaseArgs
    timeout_s = 60

    async def run(self, args: InspectDatabaseArgs, ctx: ToolContext) -> ToolOutcome:
        conn, password = await load_connection(ctx, args.connection_id)
        schema = await inspect_schema(ctx, conn, password)
        return ToolOutcome(status="ok", summary=f"{len(schema['tables'])} table(s) in {conn.name}",
                           data={"connection": conn.name, "dialect": conn.dialect, **schema})


class ValidateSQLArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=20_000)
    dialect: Literal["postgresql", "sqlite"] = "postgresql"


class ValidateSQLTool(Tool):
    name = "validate_sql"
    description = "Parse and classify SQL without executing it (syntax check + read/write/DDL classification)."
    args_model = ValidateSQLArgs
    requires_project = False

    async def run(self, args: ValidateSQLArgs, ctx: ToolContext) -> ToolOutcome:
        c = classify_sql(args.query, args.dialect)
        if c.parse_error:
            return ToolOutcome(status="error", error_code="SQL_PARSE_ERROR", summary=f"Invalid SQL: {c.parse_error}")
        return ToolOutcome(status="ok", summary=f"Valid {args.dialect} SQL ({c.category})",
                           data={"category": c.category, "warnings": c.warnings, "normalized": c.statements,
                                 "requires_approval": not c.is_read_only})
