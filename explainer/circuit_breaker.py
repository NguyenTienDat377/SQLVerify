"""
explainer/circuit_breaker.py

A minimal async circuit breaker guarding outbound LLM provider calls.

Why: explanations hit a paid third-party API (Claude by default). If that
provider is down or overloaded, retrying on every divergent verification both
burns credits/latency and piles load onto an already-failing service. The
breaker trips after a run of failures and then *fails fast* for a cooldown
window — returning control immediately instead of making doomed calls.

Provider-agnostic on purpose: it wraps whatever provider the explainer is
configured to use (see explainer/providers.py), matching the project's
multi-provider design rather than hard-coding one vendor.

States (standard three-state breaker):
  closed    — calls pass through; consecutive failures are counted.
  open       — calls short-circuit immediately; entered after
               `failure_threshold` consecutive failures, lasts `reset_timeout`
               seconds.
  half_open  — after the cooldown one trial call is allowed through; success
               closes the breaker, failure re-opens it for another cooldown.

Per-process: state lives in memory, so each worker keeps its own breaker. That
is fine for a single always-on container (the deploy target); a multi-instance
deploy would trip each worker independently, which is acceptable — the goal is
to stop *a* worker from hammering a downed provider, not to share global state.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised when a call is short-circuited because the breaker is open."""


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, reset_timeout: float = 30.0):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._opened_at: float | None = None   # monotonic time the breaker opened
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if (time.monotonic() - self._opened_at) >= self.reset_timeout:
            return "half_open"
        return "open"

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """Run `fn(*args, **kwargs)`, or raise CircuitOpenError if the breaker
        is open. A successful call closes the breaker; a raised exception counts
        as a failure (and re-opens an open breaker)."""
        async with self._lock:
            state = self.state
            if state == "open":
                remaining = self.reset_timeout - (time.monotonic() - self._opened_at)
                raise CircuitOpenError(
                    f"circuit open — skipping provider call for ~{remaining:.0f}s"
                )
            if state == "half_open":
                # Become the single trial call: re-arm the timer so any
                # concurrent callers short-circuit until this trial resolves.
                self._opened_at = time.monotonic()

        try:
            result = await fn(*args, **kwargs)
        except Exception:
            await self._record_failure()
            raise
        await self._record_success()
        return result

    async def _record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            # Open (or refresh the open window) once we hit the threshold. A
            # failed half-open trial is already at/over the threshold, so it
            # re-opens here.
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()

    async def _record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None
