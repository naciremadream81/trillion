"""
query_analytics — read-only access to the analytics Supabase (Postgres) DB.

Defense in depth (both layers matter):
  1. Connection layer: this connects as the `trillion_analytics` role, which
     has SELECT-only grants and a 5s statement_timeout (set in Supabase).
  2. Tool layer (here): the SQL validator allows only single SELECT/WITH
     statements (no chaining, no write keywords), and results are row-capped
     and JSON-serialized safely.

Three actions: query, list_tables, describe_table.

Note: the Supabase pooler requires asyncpg's statement cache to be disabled
(statement_cache_size=0) — leave it as-is.
"""

from __future__ import annotations

import datetime
import decimal
import json
import re
import uuid

import asyncpg

from .base import BaseTool

MAX_ROWS = 200

# Only single read-only statements. Anything else is refused before it ever
# reaches the database (the role can't write anyway — this is the second layer).
_ALLOWED_PREFIXES = ("select", "with")
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|vacuum|merge)\b",
    re.IGNORECASE,
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_sql(sql: str) -> str:
    """Return a cleaned, validated single SELECT/WITH statement, or raise ValueError."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise ValueError("Empty SQL.")
    if ";" in s:
        raise ValueError("Multiple statements are not allowed.")
    if not s.lower().startswith(_ALLOWED_PREFIXES):
        raise ValueError("Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(s):
        raise ValueError("Only read-only queries are allowed (write keyword detected).")
    return s


def _json_safe(v):
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


class QueryAnalyticsTool(BaseTool):
    name = "query_analytics"
    description = (
        "Query the read-only 'analytics' Postgres database (Supabase) to answer "
        "questions about its data. Use action='list_tables' to see tables, "
        "action='describe_table' with a table name to see its columns, and "
        "action='query' with a single read-only SELECT statement to get rows. "
        "Only SELECT/WITH is allowed; results are capped at "
        f"{MAX_ROWS} rows. See context/analytics-supabase-schema.md for the schema "
        "and example questions."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["query", "list_tables", "describe_table"],
                "description": "What to do.",
            },
            "sql": {
                "type": "string",
                "description": "A single read-only SELECT/WITH query. Required when action='query'.",
            },
            "table": {
                "type": "string",
                "description": "Table name. Required when action='describe_table'.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def _fetch(self, sql: str, *args):
        # New short-lived connection per call; statement_cache_size=0 is
        # required by the Supabase transaction pooler.
        conn = await asyncpg.connect(self._dsn, statement_cache_size=0)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    async def run(self, action: str = "query", sql: str = None, table: str = None, **_) -> str:
        try:
            if action == "list_tables":
                rows = await self._fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' ORDER BY table_name"
                )
                return json.dumps([r["table_name"] for r in rows])

            if action == "describe_table":
                if not table or not _IDENT.match(table):
                    return "Error: a valid 'table' name is required for describe_table."
                rows = await self._fetch(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=$1 "
                    "ORDER BY ordinal_position",
                    table,
                )
                return json.dumps([dict(r) for r in rows])

            if action == "query":
                if not sql:
                    return "Error: 'sql' is required for query."
                safe = validate_sql(sql)
                rows = await self._fetch(safe)
                capped = rows[:MAX_ROWS]
                out = [{k: _json_safe(v) for k, v in dict(r).items()} for r in capped]
                result = {"rows": out, "row_count": len(out)}
                if len(rows) > MAX_ROWS:
                    result["truncated"] = True
                    result["note"] = f"Showing first {MAX_ROWS} rows."
                return json.dumps(result, default=str)

            return f"Error: unknown action '{action}'."

        except ValueError as e:
            # SQL validation failures — safe to show the model so it can retry.
            return f"[query_analytics rejected: {e}]"
        except Exception as e:  # noqa: BLE001
            return f"[query_analytics error: {type(e).__name__}: {e}]"
