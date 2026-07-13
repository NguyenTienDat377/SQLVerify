"""Self-healing SQL repair loop, driven by SQLVerify counterexamples.

The idea: an AI agent proposes a rewritten query; SQLVerify either *proves* it
equivalent to the trusted original, or hands back a concrete counterexample
database where they diverge. That counterexample is ground truth — not an
opinion — so the agent can revise against a fact instead of a vibe, and the loop
has a decidable stop condition (`status == "equivalent"`).

    trusted v1  ─┐
                 ├─► verify ──► equivalent?  ── yes ─► done (PROVEN)
    candidate v2 ┘                │
                                  no (divergent + counterexample)
                                  ▼
                     agent.revise(candidate, counterexample, outputs) ─┐
                                  ▲                                     │
                                  └─────────────────────────────────────┘

Run it (needs a reachable SQLVerify + a real API key):

    SQLVERIFY_API_KEY=sqv_... \
    SQLVERIFY_URL=http://localhost:8000 \
        python examples/repair_loop.py

The `agent_revise` below is a TOY stand-in so the demo converges without an LLM.
Swap it for a real Claude call (see the commented block) to make it a genuine
self-healing agent.
"""
import asyncio
import os

import httpx

BASE_URL = os.environ.get("SQLVERIFY_URL", "https://sqlverify.com").rstrip("/")
API_KEY = os.environ.get("SQLVERIFY_API_KEY")
MAX_TRIES = 5


class VerifyUnavailable(RuntimeError):
    """SQLVerify could not be reached or refused the request — not a query verdict."""


async def verify(client: httpx.AsyncClient, ddl_sql: str, sql_v1: str, sql_v2: str) -> dict:
    """One call to SQLVerify's JSON endpoint — the same contract the MCP tool proxies."""
    try:
        resp = await client.post(
            f"{BASE_URL}/api/verify/text",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"ddl_sql": ddl_sql, "sql_v1": sql_v1, "sql_v2": sql_v2, "bound": 3},
        )
    except httpx.ConnectError as exc:
        raise VerifyUnavailable(
            f"could not connect to SQLVerify at {BASE_URL} — is the server running? "
            f"(start it with `uvicorn main:app --port 8000`, or set SQLVERIFY_URL)"
        ) from exc
    except httpx.HTTPError as exc:
        raise VerifyUnavailable(f"request to {BASE_URL} failed: {exc}") from exc

    if resp.status_code == 401:
        raise VerifyUnavailable("SQLVerify rejected the API key (401) — check SQLVERIFY_API_KEY.")
    if resp.status_code == 402:
        raise VerifyUnavailable("SQLVerify quota exhausted (402) for this key.")
    if resp.status_code >= 400:
        raise VerifyUnavailable(f"SQLVerify returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def agent_revise(candidate: str, counterexample: dict, v1_rows: list, v2_rows: list) -> str:
    """TOY stand-in for an AI agent.

    A real agent reads the counterexample database + what v1 returned vs what its
    own query returned on that database, reasons about the bug, and rewrites the
    query. Here we just do a naive fix so the demo terminates without an LLM.

    Replace the body with a real Claude call, e.g.:

        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        msg = await client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            messages=[{"role": "user", "content": (
                "Your SQL query is wrong. On this counterexample database:\n"
                f"{json.dumps(counterexample, indent=2)}\n"
                f"the correct query returns rows: {v1_rows}\n"
                f"but your query returned: {v2_rows}\n"
                f"Your query:\n{candidate}\n"
                "Return ONLY the corrected SQL."
            )}],
        )
        return msg.content[0].text.strip()
    """
    print("    ↳ agent sees counterexample:", counterexample)
    print(f"    ↳ correct query returns {v1_rows}, mine returned {v2_rows}")
    # Toy repair: the seeded bug is an off-by-one (`> 18` should be `>= 18`).
    return candidate.replace("> 18", ">= 18")


async def repair_loop(ddl_sql: str, trusted_v1: str, candidate: str) -> None:
    if not API_KEY:
        raise SystemExit("Set SQLVERIFY_API_KEY (a sqv_ key from the SQLVerify UI).")

    async with httpx.AsyncClient(timeout=130) as client:
        for attempt in range(1, MAX_TRIES + 1):
            print(f"\n[try {attempt}] verifying candidate:\n    {candidate}")
            try:
                r = await verify(client, ddl_sql, trusted_v1, candidate)
            except VerifyUnavailable as exc:
                raise SystemExit(f"\n⚠️  {exc}")
            status = r.get("status")

            if status == "equivalent":
                print(f"\n✅ PROVEN equivalent after {attempt} attempt(s):\n    {candidate}")
                return
            if status in ("unknown", "error"):
                # No repair signal: a timeout or unsupported SQL isn't "wrong",
                # it's "don't know" — bail rather than thrash.
                print(f"\n⚠️  stopped: status={status} — {r.get('error_message') or 'no verdict'}")
                return

            # divergent → hand the agent the ground truth and let it revise.
            print(f"    ✗ divergent: {r.get('divergence_reason')}")
            candidate = agent_revise(
                candidate,
                counterexample=r.get("counterexample_db") or {},
                v1_rows=r.get("query_v1_output") or [],
                v2_rows=r.get("query_v2_output") or [],
            )

        print(f"\n🚫 gave up after {MAX_TRIES} attempts — escalate to a human.")


if __name__ == "__main__":
    DDL = "CREATE TABLE users (id INT PRIMARY KEY, age INT, country TEXT);"
    TRUSTED = "SELECT id FROM users WHERE age >= 18 AND country = 'US'"
    # Seeded bug: `> 18` wrongly excludes 18-year-olds. SQLVerify will produce a
    # counterexample with an age-18 US user; the toy agent then repairs it.
    BROKEN = "SELECT id FROM users WHERE age > 18 AND country = 'US'"

    asyncio.run(repair_loop(DDL, TRUSTED, BROKEN))
