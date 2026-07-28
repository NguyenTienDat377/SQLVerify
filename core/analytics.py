"""
core/analytics.py

PostHog product analytics for Skolem — server-side capture only.

Why server-side: the core event (a verification run) happens in the backend and
is the *same* event across all three delivery surfaces (web / CI / MCP). A
frontend JS snippet would only ever see the web UI and would miss the CI and
agent traffic — which is exactly the usage curve needed to price the metered
MCP tier. Capturing here also means no cookies, no CSP changes, no consent flow.

Usage:
    from core.analytics import capture_verification_run
    capture_verification_run(user_id=..., status="divergent", surface="mcp", ...)

Two hard rules, both enforced below:
  1. **Never break a request.** Analytics is best-effort; every call is wrapped
     so a PostHog outage/misconfig can never fail a verification. Same spirit as
     the free-tier quota gate failing open.
  2. **No-op without POSTHOG_API_KEY.** Local dev and the test suites run with
     the key unset and capture nothing.

Env:
    POSTHOG_API_KEY   # project API key; unset → analytics disabled entirely
    POSTHOG_HOST      # optional — https://us.i.posthog.com (default) | https://eu.i.posthog.com
"""

import os
from typing import Optional

from core.logger import logger

__all__ = [
    "init_analytics",
    "shutdown_analytics",
    "identify_user",
    "capture_verification_run",
    "capture_user_login",
    "capture_user_logout",
    "capture_checkout_initiated",
    "capture_subscription_event",
    "capture_project_created",
    "capture_project_deleted",
    "capture_api_key_created",
    "capture_api_key_revoked",
    "capture_quota_exceeded",
    "capture_explanation_requested",
]

_DEFAULT_HOST = "https://us.i.posthog.com"

_client = None  # posthog.Posthog | None — None means analytics is disabled


def init_analytics() -> None:
    """Initialise the PostHog client if a key is configured. Called from the app
    lifespan. Safe to call when posthog isn't installed or no key is set."""
    global _client

    api_key = os.getenv("POSTHOG_API_KEY")
    if not api_key:
        logger.info("Analytics disabled (POSTHOG_API_KEY unset)")
        return

    try:
        from posthog import Posthog

        host = os.getenv("POSTHOG_HOST", _DEFAULT_HOST)
        _client = Posthog(
            project_api_key=api_key,
            host=host,
            # Events are queued and flushed by a background thread, so capture()
            # never blocks the request path.
            enable_exception_autocapture=True,
        )
        logger.info("Analytics enabled | host={host}", host=host)
    except Exception as exc:  # noqa: BLE001 — never let analytics break startup
        _client = None
        logger.warning("Analytics init failed, continuing without it: {err}", err=exc)


def shutdown_analytics() -> None:
    """Flush queued events on shutdown so the last runs aren't lost."""
    if _client is None:
        return
    try:
        _client.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics shutdown failed: {err}", err=exc)


def identify_user(*, user_id: str, email: Optional[str] = None) -> None:
    """Set person properties for a user. Email is PII and belongs here, not in
    event properties. Safe to call on every login — PostHog merges properties."""
    if _client is None or not user_id:
        return
    try:
        properties: dict = {}
        if email:
            properties["email"] = email
        if properties:
            _client.set(distinct_id=user_id, properties=properties)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics identify failed: {err}", err=exc)


def capture_verification_run(
    *,
    user_id: Optional[str],
    status: str,
    surface: str,
    duration_ms: int,
    bound: int,
    dialect: str,
    project_id: Optional[str] = None,
) -> None:
    """Record one verification run.

    `surface` is the delivery surface: "web" | "ci" | "mcp". Separating mcp from
    ci is what makes agent usage measurable (and therefore priceable) — they
    share the /api/verify/text endpoint, so the surface is resolved from the
    client's User-Agent (see api/verify.py:_resolve_surface).

    Anonymous (unauthenticated) runs are skipped rather than sent under a
    placeholder id, so per-account usage curves stay clean.
    """
    if _client is None or not user_id:
        return
    try:
        _client.capture(
            distinct_id=user_id,
            event="verification_run",
            properties={
                "status": status,          # equivalent | divergent | unknown | error
                "surface": surface,        # web | ci | mcp
                "duration_ms": duration_ms,
                "bound": bound,
                "dialect": dialect,
                "project_id": project_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 — analytics must never fail a verification
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_user_login(*, user_id: Optional[str], auth_method: str) -> None:
    """Record a successful login/session establishment."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(
            distinct_id=user_id,
            event="user_logged_in",
            properties={"auth_method": auth_method},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_user_logout(*, user_id: Optional[str]) -> None:
    """Record an explicit logout."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(distinct_id=user_id, event="user_logged_out")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_checkout_initiated(*, user_id: Optional[str], plan: str) -> None:
    """Record a billing checkout redirect."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(
            distinct_id=user_id,
            event="checkout_initiated",
            properties={"plan": plan},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_subscription_event(
    *,
    user_id: Optional[str],
    event_type: str,
    tier: str,
    status: str,
) -> None:
    """Record a Lemon Squeezy subscription lifecycle event."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(
            distinct_id=user_id,
            event="subscription_updated",
            properties={
                "event_type": event_type,
                "tier": tier,
                "status": status,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_project_created(*, user_id: Optional[str]) -> None:
    """Record a project creation."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(distinct_id=user_id, event="project_created")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_project_deleted(*, user_id: Optional[str]) -> None:
    """Record a project deletion."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(distinct_id=user_id, event="project_deleted")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_api_key_created(*, user_id: Optional[str]) -> None:
    """Record an API key creation."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(distinct_id=user_id, event="api_key_created")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_api_key_revoked(*, user_id: Optional[str]) -> None:
    """Record an API key revocation."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(distinct_id=user_id, event="api_key_revoked")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_quota_exceeded(
    *,
    user_id: Optional[str],
    surface: str,
    runs_used: int,
) -> None:
    """Record a free-tier quota exceeded gate."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(
            distinct_id=user_id,
            event="quota_exceeded",
            properties={
                "surface": surface,
                "runs_used": runs_used,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)


def capture_explanation_requested(*, user_id: Optional[str]) -> None:
    """Record an on-demand AI explanation request for a divergent run."""
    if _client is None or not user_id:
        return
    try:
        _client.capture(distinct_id=user_id, event="explanation_requested")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analytics capture failed: {err}", err=exc)
