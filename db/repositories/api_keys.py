"""
db/repositories/api_keys.py

Data access layer for the api_keys table — long-lived, per-user API keys for
CI/CD clients (the second auth path alongside browser sessions).

Security model:
  - The raw key (`sqv_<token>`) is shown to the user exactly ONCE, at creation.
  - Only its SHA-256 hash is stored. A 256-bit random token has no low-entropy
    structure to brute-force, so a plain hash (not bcrypt/argon2) is the correct
    and fast choice for verification on every request.
  - The service-key Supabase client bypasses RLS, so these repos scope by
    user_id in code; resolve_api_key() intentionally looks up across all users
    by hash.

Usage:
    raw, row = await create_api_key(user_id, "CI pipeline")
    user_id  = await resolve_api_key(raw)        # None if unknown/revoked
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from loguru import logger

from db.client import get_client

KEY_PREFIX = "sqv_"          # identifies our keys in an Authorization header
_DISPLAY_CHARS = 12          # how much of the raw key to keep for display


def _hash(raw_key: str) -> str:
    """SHA-256 hex digest of a raw key. Deterministic — used for both storage
    and lookup."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_raw_key() -> str:
    """A fresh, URL-safe API key: `sqv_` + 256 bits of randomness."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


async def create_api_key(user_id: str, name: str) -> tuple[Optional[str], Optional[dict]]:
    """
    Mint a new API key for a user.

    Returns:
        (raw_key, row) on success — raw_key is the ONLY time the full key is
        available; persist nothing but its hash. Returns (None, None) on failure.
    """
    raw_key = generate_raw_key()
    row = {
        "user_id":    user_id,
        "name":       (name or "API key").strip()[:100],
        "key_hash":   _hash(raw_key),
        "key_prefix": raw_key[:_DISPLAY_CHARS],
    }
    try:
        client = get_client()
        response = client.table("api_keys").insert(row).execute()
        saved = response.data[0] if response.data else None
        return raw_key, saved
    except Exception as e:
        logger.error("Failed to create API key for {uid}: {err}", uid=user_id, err=e)
        return None, None


async def list_api_keys(user_id: str) -> list[dict]:
    """List a user's keys for display — never returns the hash or raw key."""
    try:
        client = get_client()
        response = (
            client.table("api_keys")
            .select("id, name, key_prefix, created_at, last_used_at, revoked_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return response.data or []
    except Exception as e:
        logger.error("Failed to list API keys for {uid}: {err}", uid=user_id, err=e)
        return []


async def revoke_api_key(user_id: str, key_id: str) -> bool:
    """Revoke a key the user owns. Scoped by user_id so one user can't revoke
    another's key even though the service key bypasses RLS."""
    try:
        client = get_client()
        (
            client.table("api_keys")
            .update({"revoked_at": "now()"})
            .eq("id", key_id)
            .eq("user_id", user_id)
            .execute()
        )
        return True
    except Exception as e:
        logger.error("Failed to revoke API key {kid}: {err}", kid=key_id, err=e)
        return False


async def resolve_api_key(raw_key: str) -> Optional[str]:
    """
    Map a raw API key to its owning user_id, or None if unknown/revoked.

    Looks up by hash across all users (revoked keys excluded) and best-effort
    stamps last_used_at. Returns None on any failure so the caller fails closed
    (treats the request as unauthenticated).
    """
    if not raw_key or not raw_key.startswith(KEY_PREFIX):
        return None
    try:
        client = get_client()
        response = (
            client.table("api_keys")
            .select("id, user_id")
            .eq("key_hash", _hash(raw_key))
            .is_("revoked_at", "null")
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        record = response.data[0]
        try:
            client.table("api_keys").update({"last_used_at": "now()"}).eq(
                "id", record["id"]
            ).execute()
        except Exception:
            pass  # last_used_at is telemetry only — never fail auth on it
        return record["user_id"]
    except Exception as e:
        logger.error("Failed to resolve API key: {err}", err=e)
        return None
