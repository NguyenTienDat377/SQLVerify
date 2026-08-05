"""
tests/billing_test.py

Tests for Lemon Squeezy billing:
  - free-tier quota gate (_enforce_quota) on POST /api/verify/text
  - /billing/checkout redirect carries user_id + email as LS custom data
  - /billing/portal fetches a fresh portal URL from the LS API (httpx stubbed)
  - webhook subscription_payment_failed → upsert with status past_due + user_id

External deps (Supabase, LS API, httpx, Z3) are stubbed; no services contacted.

Run directly (no pytest needed):
    .venv/bin/python tests/billing_test.py
"""

import os
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SUPABASE_URL", "https://demo.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key")
os.environ["LS_INDIVIDUAL_CHECKOUT_URL"] = "https://store.lemonsqueezy.com/checkout/buy/IND"
os.environ["LS_TEAM_CHECKOUT_URL"] = "https://store.lemonsqueezy.com/checkout/buy/TEAM"
os.environ["LEMONSQUEEZY_API_KEY"] = "ls-api-key"

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import api.verify as verify_mod
import api.billing as billing_mod
import api.webhooks as wh_mod
from core.models import VerificationResult

# Identity the test middleware injects (tests mutate this).
_ctx = {"user_id": "user-1", "email": "dev@example.com"}


def _set_identity_middleware(app):
    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.state.user_id = _ctx["user_id"]
        request.state.user_email = _ctx["email"]
        return await call_next(request)


# ── Verify app (quota gate) ──────────────────────────────────────────────────

async def _ok_save(*a, **k):
    return "run-id"

async def _no_explain(*a, **k):
    return ""

verify_mod.save_run = _ok_save
verify_mod.explain_result = _no_explain
verify_mod.check_equivalence = lambda **k: VerificationResult(status="equivalent")
verify_mod.limiter.enabled = False

_vapp = FastAPI()
_vapp.state.limiter = verify_mod.limiter
_set_identity_middleware(_vapp)
_vapp.include_router(verify_mod.router)
_vclient = TestClient(_vapp)

_VERIFY_BODY = {"ddl_sql": "CREATE TABLE t (id INTEGER PRIMARY KEY);",
                "sql_v1": "SELECT id FROM t", "sql_v2": "SELECT id FROM t"}


def _stub_quota(used=0, sub=None, raise_on_count=False):
    async def fake_sub(user_id):
        return sub
    async def fake_count(user_id):
        if raise_on_count:
            raise RuntimeError("db down")
        return used
    verify_mod.get_active_subscription_by_user = fake_sub
    verify_mod.count_runs_this_month = fake_count


def test_under_limit_allows_verification():
    _stub_quota(used=5, sub=None)
    r = _vclient.post("/api/verify/text", json=_VERIFY_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "equivalent"


def test_at_limit_unpaid_returns_402_json():
    _stub_quota(used=verify_mod.FREE_TIER_MONTHLY_LIMIT, sub=None)
    r = _vclient.post("/api/verify/text", json=_VERIFY_BODY)
    assert r.status_code == 402, r.text
    assert r.json()["error"] == "quota_exceeded"


def test_at_limit_htmx_returns_402_partial():
    _stub_quota(used=verify_mod.FREE_TIER_MONTHLY_LIMIT, sub=None)
    r = _vclient.post("/api/verify/text", json=_VERIFY_BODY, headers={"hx-request": "true"})
    assert r.status_code == 402, r.text
    assert "free tier" in r.text.lower()


def test_team_plan_bypasses_limit_entirely():
    # Only Team is truly unlimited — see CLAUDE.md's Team-tier decision table,
    # item #1 (Individual is capped so the volume lever isn't given away at $9).
    _stub_quota(used=10_000, sub={"tier": "team", "status": "active"})
    r = _vclient.post("/api/verify/text", json=_VERIFY_BODY)
    assert r.status_code == 200, r.text


def test_individual_plan_allows_under_its_cap():
    _stub_quota(used=verify_mod.INDIVIDUAL_TIER_MONTHLY_LIMIT - 1,
                sub={"tier": "individual", "status": "active"})
    r = _vclient.post("/api/verify/text", json=_VERIFY_BODY)
    assert r.status_code == 200, r.text


def test_individual_plan_capped_at_its_limit():
    _stub_quota(used=verify_mod.INDIVIDUAL_TIER_MONTHLY_LIMIT,
                sub={"tier": "individual", "status": "active"})
    r = _vclient.post("/api/verify/text", json=_VERIFY_BODY)
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["error"] == "quota_exceeded"
    assert body["limit"] == verify_mod.INDIVIDUAL_TIER_MONTHLY_LIMIT


def test_quota_fails_open_on_error():
    _stub_quota(raise_on_count=True, sub=None)
    r = _vclient.post("/api/verify/text", json=_VERIFY_BODY)
    assert r.status_code == 200, "a DB hiccup must not block the user"


# ── Billing app (checkout + portal) ──────────────────────────────────────────

_bapp = FastAPI()
_set_identity_middleware(_bapp)
_bapp.include_router(billing_mod.router)
_bclient = TestClient(_bapp, follow_redirects=False)


def test_checkout_redirects_with_custom_user_id_and_email():
    r = _bclient.get("/billing/checkout?plan=team")
    assert r.status_code == 303, r.text
    loc = unquote(r.headers["location"])
    assert loc.startswith("https://store.lemonsqueezy.com/checkout/buy/TEAM"), loc
    assert "checkout[custom][user_id]=user-1" in loc, loc
    assert "checkout[email]=dev@example.com" in loc, loc


def test_checkout_unknown_plan_400():
    r = _bclient.get("/billing/checkout?plan=enterprise")
    assert r.status_code == 400, r.text


def test_portal_redirects_to_ls_portal_url():
    async def fake_sub(user_id):
        return {"subscription_id": "sub_123"}
    billing_mod.get_active_subscription_by_user = fake_sub

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": {"attributes": {"urls": {"customer_portal": "https://store.lemonsqueezy.com/portal/xyz"}}}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            return _Resp()

    class _Httpx:
        AsyncClient = _Client
        HTTPError = Exception

    real = billing_mod.httpx
    billing_mod.httpx = _Httpx
    try:
        r = _bclient.get("/billing/portal")
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "https://store.lemonsqueezy.com/portal/xyz"
    finally:
        billing_mod.httpx = real


# ── Webhook payment_failed ───────────────────────────────────────────────────

def test_webhook_payment_failed_sets_past_due_with_user_id():
    captured = {}

    async def fake_upsert(**kwargs):
        captured.update(kwargs)
        return "row-id"

    wh_mod.upsert_subscription = fake_upsert
    wh_mod._verify_signature = lambda body, sig: True  # bypass HMAC in test

    app = FastAPI()
    app.include_router(wh_mod.router)
    client = TestClient(app)

    payload = {
        "meta": {"event_name": "subscription_payment_failed",
                 "custom_data": {"user_id": "user-9"}},
        "data": {"id": "55", "attributes": {
            "customer_id": 1, "user_email": "x@y.com", "order_id": 2,
            "product_id": 3, "variant_id": "999", "status": "past_due",
            "renews_at": None}},
    }
    r = client.post("/api/webhooks/lemonsqueezy", json=payload,
                    headers={"X-Signature": "whatever"})
    assert r.status_code == 200, r.text
    assert captured.get("status") == "past_due", captured
    assert captured.get("user_id") == "user-9", captured


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
