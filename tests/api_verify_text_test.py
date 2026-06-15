"""
tests/api_verify_text_test.py

End-to-end tests for the CI/CD JSON endpoint POST /api/verify/text.

Exercises the real endpoint code (routing, request validation, the call into
check_equivalence, per-surface timeout wiring, and response shaping) through a
FastAPI TestClient. The two external dependencies are stubbed so no real
services are needed:

  - save_run()       → Supabase: replaced with a recorder (also pins the
                       argument-passing bug where the handler referenced
                       undefined locals instead of body.* fields).
  - explain_result() → LLM provider: replaced with a recorder.

Z3 runs for real on a tiny schema, so an 'equivalent' / 'divergent' verdict is
a genuine end-to-end check.

Run directly (no pytest needed):
    .venv/bin/python tests/api_verify_text_test.py
or with pytest:
    .venv/bin/python -m pytest tests/api_verify_text_test.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import api.verify as verify_mod
from core.models import VerificationResult

# ── Stub the external dependencies (Supabase + LLM) ──────────────────────────

_REAL_CHECK = verify_mod.check_equivalence

calls: dict[str, list] = {"save_run": [], "explain": [], "check": []}


async def _fake_save_run(result, ddl_sql, sql_v1, sql_v2, dialect, duration_ms,
                         user_id=None):
    calls["save_run"].append({
        "status": result.status, "ddl": ddl_sql, "v1": sql_v1, "v2": sql_v2,
        "dialect": dialect, "duration_ms": duration_ms,
    })
    return "00000000-0000-0000-0000-000000000000"


async def _fake_explain(result, sql_v1, sql_v2, provider_name=None):
    calls["explain"].append({"v1": sql_v1, "v2": sql_v2})
    return "stub explanation"


def _capturing_check(**kwargs):
    """Drop-in for check_equivalence that records kwargs (timeout wiring)."""
    calls["check"].append(kwargs)
    return VerificationResult(status="equivalent")


verify_mod.save_run = _fake_save_run
verify_mod.explain_result = _fake_explain

# Mirror main.py's rate-limit wiring on the test app so @limiter.limit works.
# Disabled by default so the functional tests below aren't throttled; the
# dedicated 429 test re-enables it.
_app = FastAPI()
_app.state.limiter = verify_mod.limiter
_app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
_app.include_router(verify_mod.router)
verify_mod.limiter.enabled = False
client = TestClient(_app)

URL = "/api/verify/text"

DDL = "CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, balance INTEGER);"
Q_GT_100 = "SELECT account_id FROM accounts WHERE balance > 100"
Q_GT_200 = "SELECT account_id FROM accounts WHERE balance > 200"


def _reset():
    calls["save_run"].clear()
    calls["explain"].clear()
    calls["check"].clear()
    verify_mod.check_equivalence = _REAL_CHECK


# ── Tests ────────────────────────────────────────────────────────────────────

def test_equivalent_pair_returns_200_and_persists():
    """A real equivalent verdict round-trips, and save_run receives the body
    fields (the regression that previously raised NameError)."""
    _reset()
    resp = client.post(URL, json={
        "ddl_sql": DDL, "sql_v1": Q_GT_100, "sql_v2": Q_GT_100,
        "dialect": "sqlite",
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    assert resp.json()["status"] == "equivalent", resp.json()
    # save_run was called exactly once with the request's own SQL — not stale
    # or undefined locals.
    assert len(calls["save_run"]) == 1, calls["save_run"]
    saved = calls["save_run"][0]
    assert saved["ddl"] == DDL and saved["v1"] == Q_GT_100 \
        and saved["v2"] == Q_GT_100 and saved["dialect"] == "sqlite"


def test_divergent_pair_is_explained_and_persisted():
    """A real divergent verdict returns a counterexample, triggers the
    explainer, and still persists."""
    _reset()
    resp = client.post(URL, json={
        "ddl_sql": DDL, "sql_v1": Q_GT_100, "sql_v2": Q_GT_200,
        "dialect": "sqlite",
    })
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["status"] == "divergent", body
    assert body["counterexample_db"], "expected a counterexample DB"
    assert len(calls["explain"]) == 1, "explainer must run on divergent results"
    assert len(calls["save_run"]) == 1


def test_default_timeout_is_cicd_60s():
    """Omitting timeout_ms uses the CI/CD default (60s), not the web 15s."""
    _reset()
    verify_mod.check_equivalence = _capturing_check
    try:
        resp = client.post(URL, json={
            "ddl_sql": DDL, "sql_v1": Q_GT_100, "sql_v2": Q_GT_100,
        })
        assert resp.status_code == 200, resp.text
        assert calls["check"][0]["timeout_ms"] == verify_mod.CICD_TIMEOUT_MS
        assert verify_mod.CICD_TIMEOUT_MS == 60_000
    finally:
        _reset()


def test_caller_timeout_clamped_to_ceiling():
    """An over-large caller timeout is clamped to the hard ceiling, not 400'd."""
    _reset()
    verify_mod.check_equivalence = _capturing_check
    try:
        resp = client.post(URL, json={
            "ddl_sql": DDL, "sql_v1": Q_GT_100, "sql_v2": Q_GT_100,
            "timeout_ms": 999_999,
        })
        assert resp.status_code == 200, resp.text
        assert calls["check"][0]["timeout_ms"] == verify_mod.MAX_TIMEOUT_MS
        assert verify_mod.MAX_TIMEOUT_MS == 120_000
    finally:
        _reset()


def test_bound_over_max_is_rejected():
    """bound beyond MAX_BOUND is a 400 before any solving happens."""
    _reset()
    resp = client.post(URL, json={
        "ddl_sql": DDL, "sql_v1": Q_GT_100, "sql_v2": Q_GT_100,
        "bound": verify_mod.MAX_BOUND + 1,
    })
    assert resp.status_code == 400, f"{resp.status_code}: {resp.text}"
    assert len(calls["save_run"]) == 0


def test_rate_limit_returns_429_after_limit():
    """With the limiter enabled, the (N+1)th request from one IP gets 429.
    Derives N from VERIFY_RATE_LIMIT so it stays in sync with the constant."""
    _reset()
    verify_mod.check_equivalence = _capturing_check   # no Z3, fast loop
    limit = int(verify_mod.VERIFY_RATE_LIMIT.split("/")[0])
    verify_mod.limiter.enabled = True
    verify_mod.limiter.reset()
    try:
        codes = []
        for _ in range(limit + 1):
            r = client.post(URL, json={
                "ddl_sql": DDL, "sql_v1": Q_GT_100, "sql_v2": Q_GT_100})
            codes.append(r.status_code)
        assert codes.count(200) == limit, codes
        assert codes[-1] == 429, codes
    finally:
        verify_mod.limiter.enabled = False
        verify_mod.limiter.reset()
        _reset()


# ── Runner (no pytest required) ──────────────────────────────────────────────

def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}\n        {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {t.__name__}\n        {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
