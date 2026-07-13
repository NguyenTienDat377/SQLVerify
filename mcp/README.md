# SQLVerify MCP server

A thin [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude
Code, Claude Desktop, Cursor, …) call SQLVerify **in-loop** to formally prove
that a rewritten SQL query is equivalent to the original — or get a concrete
counterexample database where they differ.

It holds no Z3 solver. It forwards each call to a running SQLVerify's
`POST /api/verify/text`, authenticated with a per-user `sqv_` API key. The engine
stays on the server; this proxy runs wherever the agent runs.

This lives inside the SQLVerify repo (versioned with the API contract it proxies)
but is self-contained — it only needs `mcp` + `httpx`, not the app's deps.

## Exposed tool

`verify_sql_equivalence(ddl_sql, sql_v1, sql_v2, bound=3)` → returns SQLVerify's
`VerifyResponse` JSON verbatim:

| field | meaning |
|---|---|
| `status` | `equivalent` \| `divergent` \| `unknown` (timeout — **not** equivalent) \| `error` (unsupported SQL / bad input) |
| `divergence_reason` | plain-English summary when divergent |
| `counterexample_db` | `{table: [rows]}` database that makes the two queries differ |
| `query_v1_output` / `query_v2_output` | each query's rows on that database |
| `explanation` | human-readable LLM explanation (for relaying to a person; agents should repair from the structured counterexample) |
| `error_message` | populated only when `status == "error"` |

## Setup

```bash
cd mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Mint a `sqv_…` key in the SQLVerify UI (**Keys** page).

## Connect it to Claude

Use **absolute paths** (the client's working directory isn't your repo).

### Claude Code

```bash
claude mcp add sqlverify \
  --env SQLVERIFY_API_KEY=sqv_yourkey \
  --env SQLVERIFY_URL=https://sqlverify.com \
  -- /abs/path/to/SQLVerify/mcp/.venv/bin/python \
     /abs/path/to/SQLVerify/mcp/sqlverify_mcp.py
```

### Claude Desktop

`claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`):

```json
{
  "mcpServers": {
    "sqlverify": {
      "command": "/abs/path/to/SQLVerify/mcp/.venv/bin/python",
      "args": ["/abs/path/to/SQLVerify/mcp/sqlverify_mcp.py"],
      "env": {
        "SQLVERIFY_API_KEY": "sqv_yourkey",
        "SQLVERIFY_URL": "https://sqlverify.com"
      }
    }
  }
}
```

## Test it without an agent

```bash
SQLVERIFY_API_KEY=sqv_yourkey \
  npx @modelcontextprotocol/inspector \
  .venv/bin/python sqlverify_mcp.py
```

Opens a UI to see the advertised tool and fire test calls.

## Self-healing repair loop

`examples/repair_loop.py` is the headline use case: an agent proposes a query,
SQLVerify proves it or returns a counterexample, and the agent revises against
that ground truth until it's proven equivalent (or a budget runs out).

```bash
SQLVERIFY_API_KEY=sqv_... SQLVERIFY_URL=http://localhost:8000 \
  python examples/repair_loop.py
```

It ships with a **toy** `agent_revise` so it converges without an LLM; swap that
for a real Claude call (a template is in the file) to get a genuine self-healing
agent. Key discipline the loop encodes:

- `equivalent` → **stop, proven.** It's a decidable win, not a vibe.
- `divergent` → feed the agent `counterexample_db` + both row-sets (raw facts),
  not the prose `explanation`.
- `unknown` / `error` → **bail**, don't thrash — those aren't repair signals.
- Cap iterations; re-verify the winner at a higher `bound` for high stakes
  (an `equivalent` verdict is only sound within the bound, default 3).

## Environment variables

| var | required | default | notes |
|---|---|---|---|
| `SQLVERIFY_API_KEY` | yes | — | per-user `sqv_` key; calls metered against its quota |
| `SQLVERIFY_URL` | no | `https://sqlverify.com` | point at `http://localhost:8000` for local dev |
