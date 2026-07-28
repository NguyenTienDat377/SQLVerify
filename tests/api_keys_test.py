"""
tests/api_keys_test.py

Tests for the per-user API-key auth path and magic-link route:
  - key generation / hashing helpers (pure, no DB)
  - JWTMiddleware dual auth path (session JWT OR per-user API key) via a
    Starlette TestClient with stubbed token/key resolution
  - POST /auth/magic-link calls the Supabase OTP endpoint correctly (httpx stub)

Run directly (no pytest needed):
    .venv/bin/python tests/api_keys_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Env that import paths / routes read (no real services are contacted).
os.environ.setdefault("SUPABASE_URL", "https://demo.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-xyz")
os.environ.setdefault("SITE_URL", "https://skolem.test")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import auth.middleware as mw
from db.repositories.api_keys import KEY_PREFIX, _hash, generate_raw_key


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_generate_raw_key_format():
    k = generate_raw_key()
    assert k.startswith(KEY_PREFIX), k
    assert len(k) > 20, "key should carry real entropy"
    assert generate_raw_key() != generate_raw_key(), "keys must be unique"


def test_hash_is_deterministic_and_not_the_raw_key():
    k = "skm_example"
    assert _hash(k) == _hash(k)
    assert _hash(k) != k
    assert len(_hash(k)) == 64, "sha256 hex digest is 64 chars"


# ── Middleware dual auth path ────────────────────────────────────────────────

def _build_app():
    app = FastAPI()
    app.add_middleware(mw.JWTMiddleware)

    @app.get("/api/probe")
    async def probe(request: Request):
        return {
            "user_id": getattr(request.state, "user_id", None),
            "method": getattr(request.state, "auth_method", None),
        }

    @app.get("/")
    async def home():
        return {"ok": True}

    return TestClient(app)


def _stub_resolution(valid_key="skm_goodkey", key_user="key-user-1",
                     valid_jwt="goodjwt", jwt_user="jwt-user-1"):
    """Patch the middleware's key + JWT resolvers; clear the TTL cache."""
    mw._api_key_cache.clear()

    async def fake_resolve(raw):
        return key_user if raw == valid_key else None

    def fake_decode(token):
        if token == valid_jwt:
            return {"sub": jwt_user, "email": "u@example.com"}
        return None

    mw.resolve_api_key = fake_resolve
    mw._decode_token = fake_decode


def test_public_path_passes_without_credentials():
    _stub_resolution()
    client = _build_app()
    r = client.get("/")
    assert r.status_code == 200, r.text


def test_protected_api_requires_credentials():
    _stub_resolution()
    client = _build_app()
    r = client.get("/api/probe")
    assert r.status_code == 401, r.text


def test_api_key_bearer_resolves_user():
    _stub_resolution()
    client = _build_app()
    r = client.get("/api/probe", headers={"Authorization": "Bearer skm_goodkey"})
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": "key-user-1", "method": "api_key"}


def test_api_key_x_header_resolves_user():
    _stub_resolution()
    client = _build_app()
    r = client.get("/api/probe", headers={"X-API-Key": "skm_goodkey"})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == "key-user-1"


def test_unknown_api_key_is_401():
    _stub_resolution()
    client = _build_app()
    r = client.get("/api/probe", headers={"Authorization": "Bearer skm_nope"})
    assert r.status_code == 401, r.text


def test_session_jwt_still_works():
    _stub_resolution()
    client = _build_app()
    # via cookie
    r = client.get("/api/probe", cookies={"sb-access-token": "goodjwt"})
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": "jwt-user-1", "method": "session"}
    # via plain (non-sqv) Bearer
    r2 = client.get("/api/probe", headers={"Authorization": "Bearer goodjwt"})
    assert r2.json()["user_id"] == "jwt-user-1"


# ── Magic link ───────────────────────────────────────────────────────────────

def test_magic_link_calls_supabase_otp():
    import api.auth as auth_mod

    calls = []

    class _FakeResp:
        status_code = 200
        text = ""

    class _FakeAsyncClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, params=None, headers=None, json=None):
            calls.append({"url": url, "params": params, "headers": headers, "json": json})
            return _FakeResp()

    class _FakeHttpx:
        AsyncClient = _FakeAsyncClient
        HTTPError = Exception

    real_httpx = auth_mod.httpx
    auth_mod.httpx = _FakeHttpx
    try:
        app = FastAPI()
        app.include_router(auth_mod.router)
        client = TestClient(app)
        r = client.post("/auth/magic-link", data={"email": "dev@example.com"})
        assert r.status_code == 200, r.text
        assert len(calls) == 1, calls
        c = calls[0]
        assert c["url"].endswith("/auth/v1/otp"), c["url"]
        assert c["params"]["redirect_to"].endswith("/auth/callback"), c["params"]
        assert c["headers"]["apikey"] == os.environ["SUPABASE_ANON_KEY"]
        assert c["json"]["email"] == "dev@example.com"
        assert c["json"]["create_user"] is True
    finally:
        auth_mod.httpx = real_httpx


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
