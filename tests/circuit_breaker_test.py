"""
tests/circuit_breaker_test.py

Unit tests for explainer/circuit_breaker.py — the async breaker that stops the
explainer from hammering a downed LLM provider.

Run directly (no pytest needed):
    .venv/bin/python tests/circuit_breaker_test.py
or with pytest:
    .venv/bin/python -m pytest tests/circuit_breaker_test.py -v
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from explainer.circuit_breaker import CircuitBreaker, CircuitOpenError


class _Stub:
    """An awaitable callable that counts invocations and can fail on demand."""

    def __init__(self):
        self.calls = 0
        self.fail = False

    async def __call__(self, value="ok"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return value


# ── Tests ────────────────────────────────────────────────────────────────────

def test_closed_passes_through_and_returns_value():
    async def go():
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=1.0)
        stub = _Stub()
        out = await cb.call(stub, "hello")
        assert out == "hello", out
        assert cb.state == "closed"
        assert stub.calls == 1
    asyncio.run(go())


def test_trips_open_after_threshold_and_short_circuits():
    async def go():
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=60.0)
        stub = _Stub()
        stub.fail = True
        # 3 failures trip the breaker
        for _ in range(3):
            try:
                await cb.call(stub)
            except RuntimeError:
                pass
        assert cb.state == "open", cb.state
        assert stub.calls == 3
        # 4th call short-circuits — the provider is NOT invoked
        try:
            await cb.call(stub)
            assert False, "expected CircuitOpenError"
        except CircuitOpenError:
            pass
        assert stub.calls == 3, "open breaker must not call the provider"
    asyncio.run(go())


def test_success_resets_failure_count():
    async def go():
        cb = CircuitBreaker(failure_threshold=3, reset_timeout=60.0)
        stub = _Stub()
        stub.fail = True
        for _ in range(2):                 # 2 failures (below threshold)
            try:
                await cb.call(stub)
            except RuntimeError:
                pass
        stub.fail = False
        await cb.call(stub)                # success resets the counter
        stub.fail = True
        for _ in range(2):                 # 2 more failures — still below threshold
            try:
                await cb.call(stub)
            except RuntimeError:
                pass
        assert cb.state == "closed", "counter should have reset after success"
    asyncio.run(go())


def test_half_open_trial_success_closes():
    async def go():
        cb = CircuitBreaker(failure_threshold=2, reset_timeout=0.05)
        stub = _Stub()
        stub.fail = True
        for _ in range(2):
            try:
                await cb.call(stub)
            except RuntimeError:
                pass
        assert cb.state == "open"
        await asyncio.sleep(0.06)          # cooldown elapses
        assert cb.state == "half_open"
        stub.fail = False
        out = await cb.call(stub, "recovered")   # trial call succeeds
        assert out == "recovered"
        assert cb.state == "closed"
    asyncio.run(go())


def test_half_open_trial_failure_reopens():
    async def go():
        cb = CircuitBreaker(failure_threshold=2, reset_timeout=0.05)
        stub = _Stub()
        stub.fail = True
        for _ in range(2):
            try:
                await cb.call(stub)
            except RuntimeError:
                pass
        await asyncio.sleep(0.06)
        assert cb.state == "half_open"
        try:                                # trial call fails again
            await cb.call(stub)
        except RuntimeError:
            pass
        assert cb.state == "open", "failed trial must re-open the breaker"
    asyncio.run(go())


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
