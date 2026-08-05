"""Skolem MCP server — a thin stdio proxy.

Exposes Skolem's formal SQL-equivalence engine as an MCP tool that AI agents
(Claude Code, Claude Desktop, Cursor, etc.) can call in-loop. It holds no solver
itself: it forwards to a hosted Skolem instance's POST /api/verify/text
endpoint, authenticated with the user's per-user `skm_` API key.

Run locally over stdio:
    SKOLEM_API_KEY=skm_... python skolem_mcp.py

Or point the MCP client's command at this file (see README.md).
"""
import os

import httpx
from mcp.server.mcpserver import MCPServer

# Where the hosted Skolem lives. Override for local dev (http://localhost:8000).
BASE_URL = os.environ.get("SKOLEM_URL", "https://skolem.dev").rstrip("/")

# A per-user API key minted in the Skolem UI (Keys page). Required — the
# endpoint is metered against this user's quota, so we fail fast without one.
API_KEY = os.environ.get("SKOLEM_API_KEY")

# httpx timeout must sit above Skolem's CI ceiling (120s solve) plus slack.
_HTTP_TIMEOUT_S = 130.0

# Identifies agent traffic to the server. /api/verify/text is shared with CI/CD
# clients, so this User-Agent is the only thing that lets Skolem tell in-loop
# AI-agent calls apart from pipeline calls (see api/verify.py:_resolve_surface).
# Analytics-only — it never affects auth, quota, or the verdict.
_VERSION = "0.1.0"
_USER_AGENT = f"skolem-mcp/{_VERSION}"

mcp = MCPServer("skolem")


@mcp.tool()
async def verify_sql_equivalence(
    ddl_sql: str,
    sql_v1: str,
    sql_v2: str,
    bound: int = 3,
) -> dict:
    """Formally prove whether two SQL SELECT queries are semantically equivalent
    under a given schema, using Z3/SMT solving.

    Use this to check an AI-rewritten query against the original BEFORE trusting
    it: unlike running the query, this proves the rewrite preserves meaning, or
    returns a concrete counterexample database where the two queries diverge.

    Returns the raw Skolem result JSON:
      - status: "equivalent" | "divergent" | "unknown" | "error"
          equivalent → the rewrite is safe (within the bound).
          divergent  → they differ; see counterexample_db + the query outputs.
          unknown    → solver timed out (no verdict — do NOT treat as equivalent).
          error      → unsupported SQL / bad input; see error_message.
      - divergence_reason: plain-English summary when divergent.
      - counterexample_db: {table: [rows]} database that makes them differ.
      - query_v1_output / query_v2_output: each query's result on that database.
      - error_message: populated only when status is "error".

    Supported SQL subset — anything outside it is rejected as status "error",
    never silently ignored (a dropped clause could turn a real divergence into a
    false "equivalent"):
      - A single SELECT over plain tables. SELECT list: plain columns, SUM(col),
        COUNT(*), COUNT(col), COALESCE(aggregate, literal).
      - JOINs: any chain mixing INNER / LEFT / RIGHT / FULL, each ON a single
        column equality. No CROSS joins and no self-joins.
      - WHERE / HAVING: full AND / OR / NOT trees over comparisons (column vs
        literal and column vs column), IS [NOT] NULL, and IN (value-list), all
        under three-valued (NULL-aware) logic.
      - WHERE also supports `col IN (SELECT ...)` for an uncorrelated,
        single-column subquery.
      - GROUP BY on plain columns from any joined table; HAVING on aggregate
        comparisons.
      - Non-recursive CTEs (WITH); a CTE body may itself join/group/aggregate.
      - ORDER BY is accepted but ignored (equivalence is under bag semantics).

    Rejected (status "error"): window functions, UNION, SELECT DISTINCT,
    SELECT *, LIMIT / OFFSET, BETWEEN, LIKE, scalar subqueries, EXISTS, derived
    tables in FROM, IN (SELECT ...) in HAVING, WITH RECURSIVE, a CTE body
    referencing another CTE, a CTE combined with an outer join, ordering
    comparisons (< >) on TEXT/TIMESTAMP columns, and boolean literals in
    predicates (e.g. `WHERE active = TRUE`).

    Args:
        ddl_sql: Flyway-style CREATE TABLE DDL defining the schema. ALTER TABLE
                 statements are folded in, so a concatenated migrations
                 directory works.
        sql_v1:  The original / trusted SQL SELECT query.
        sql_v2:  The rewritten (e.g. AI-generated) query to check against v1.
        bound:   Max rows per table Z3 explores (default 3, max 6). An
                 "equivalent" verdict is sound only within this bound; raising it
                 finds rarer bugs but is slower and may time out (status
                 "unknown"). Cost grows roughly bound^(number of joined tables),
                 so raise it only after a run comes back equivalent at 3.
    """
    if not API_KEY:
        return {
            "status": "error",
            "error_message": (
                "SKOLEM_API_KEY is not set. Mint a key in the Skolem UI "
                "(Keys page) and pass it to this MCP server via the "
                "SKOLEM_API_KEY environment variable."
            ),
        }

    payload = {"ddl_sql": ddl_sql, "sql_v1": sql_v1, "sql_v2": sql_v2, "bound": bound}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            resp = await client.post(
                f"{BASE_URL}/api/verify/text",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "User-Agent": _USER_AGENT,
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        # Network/transport failure reaching Skolem — surface it as a tool
        # error rather than raising, so the agent gets an actionable message.
        return {
            "status": "error",
            "error_message": f"Could not reach Skolem at {BASE_URL}: {exc}",
        }

    if resp.status_code == 402:
        return {
            "status": "error",
            "error_message": (
                "Skolem free-tier monthly quota exhausted for this API key. "
                "Upgrade the plan or wait for the next UTC calendar month."
            ),
        }
    if resp.status_code == 401:
        return {
            "status": "error",
            "error_message": "Skolem rejected the API key (401). Check SKOLEM_API_KEY.",
        }
    if resp.status_code == 429:
        return {
            "status": "error",
            "error_message": "Skolem rate limit hit (429). Slow down and retry shortly.",
        }

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return {
            "status": "error",
            "error_message": f"Skolem returned HTTP {resp.status_code}: {exc}",
        }

    return resp.json()  # VerifyResponse, verbatim


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
