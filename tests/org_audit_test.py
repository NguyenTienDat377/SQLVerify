"""
tests/org_audit_test.py

Tests for the org audit CSV export (CLAUDE.md Team-tier decision table,
item #6): GET /api/organizations/{org_id}/audit.csv.

External deps (Supabase, GoTrue admin) are stubbed; no services contacted.

Run directly (no pytest needed):
    .venv/bin/python tests/org_audit_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SUPABASE_URL", "https://demo.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import api.organizations as org_mod

_ROWS = [
    {"id": "run-1", "created_at": "2026-08-01T00:00:00Z", "status": "divergent",
     "divergence_reason": "=cmd|/c calc", "duration_ms": 120, "user_id": "user-a",
     "project_id": "proj-1", "projects": {"name": "Payments", "org_id": "org-1"}},
    {"id": "run-2", "created_at": "2026-08-02T00:00:00Z", "status": "equivalent",
     "divergence_reason": None, "duration_ms": 50, "user_id": "user-b",
     "project_id": "proj-1", "projects": {"name": "Payments", "org_id": "org-1"}},
]

_ctx = {"user_id": "owner-1"}


def _set_identity_middleware(app):
    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.user_id = _ctx["user_id"]
        return await call_next(request)


async def _fake_get_org_if_owner(user_id, org_id):
    if user_id == "owner-1" and org_id == "org-1":
        return {"id": "org-1", "owner_id": "owner-1", "name": "Acme", "seat_limit": 5}
    return None


async def _fake_audit_rows(org_id, limit=5000):
    return _ROWS if org_id == "org-1" else []


async def _fake_email(uid):
    return {"user-a": "a@x.com", "user-b": "b@x.com"}.get(uid, uid)


org_mod.get_organization_if_owner = _fake_get_org_if_owner
org_mod.get_org_audit_rows = _fake_audit_rows
org_mod.get_email_by_id = _fake_email

_app = FastAPI()
_set_identity_middleware(_app)
_app.include_router(org_mod.router)
_client = TestClient(_app)


def test_owner_gets_csv_with_expected_rows():
    _ctx["user_id"] = "owner-1"
    r = _client.get("/api/organizations/org-1/audit.csv")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="skolem-audit-org-1.csv"' in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0] == "run_id,created_at,user_email,project,status,divergence_reason,duration_ms"
    assert len(lines) == 3  # header + 2 rows
    assert "a@x.com" in lines[1] and "Payments" in lines[1]
    assert "b@x.com" in lines[2]


def test_non_owner_gets_404():
    _ctx["user_id"] = "someone-else"
    r = _client.get("/api/organizations/org-1/audit.csv")
    assert r.status_code == 404, r.text


def test_unknown_org_gets_404():
    _ctx["user_id"] = "owner-1"
    r = _client.get("/api/organizations/org-does-not-exist/audit.csv")
    assert r.status_code == 404, r.text


def test_formula_injection_is_neutralized():
    _ctx["user_id"] = "owner-1"
    r = _client.get("/api/organizations/org-1/audit.csv")
    assert r.status_code == 200, r.text
    # run-1's divergence_reason starts with '=' — must be defused with a
    # leading quote so a spreadsheet app doesn't execute it as a formula.
    assert "'=cmd|/c calc" in r.text, r.text
    assert ",=cmd" not in r.text, "formula trigger must not reach the cell unescaped"


def test_empty_audit_still_returns_header_only():
    _ctx["user_id"] = "owner-1"

    async def _empty(org_id, limit=5000):
        return []
    org_mod.get_org_audit_rows = _empty
    try:
        r = _client.get("/api/organizations/org-1/audit.csv")
        assert r.status_code == 200, r.text
        assert r.text.strip() == "run_id,created_at,user_email,project,status,divergence_reason,duration_ms"
    finally:
        org_mod.get_org_audit_rows = _fake_audit_rows


# ── Runner (no pytest required) ──────────────────────────────────────────────

def _run_all() -> int:
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
    sys.exit(_run_all())
