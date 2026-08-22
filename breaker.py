"""Per-engine health tracking and circuit breaking for polite search fallback.

This module deliberately treats rate limits, verification pages, and transient network
failures as signals to reduce traffic.  It does not attempt to evade access controls.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _State:
    failures: int = 0
    blocked_until: float = 0.0
    last_error: str = ""


class EngineCircuitBreaker:
    """In-memory, process-local circuit breaker for individual search engines."""

    def __init__(self, fail_threshold: int = 3, cooldown_sec: float = 300.0):
        self.fail_threshold = max(1, int(fail_threshold))
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self._states: dict[str, _State] = {}
        self._lock = asyncio.Lock()

    async def allow(self, engine: str) -> bool:
        """Return False while an engine is in its cooldown window."""
        async with self._lock:
            state = self._states.get(engine)
            if not state:
                return True
            return time.monotonic() >= state.blocked_until

    async def record_success(self, engine: str) -> None:
        async with self._lock:
            self._states[engine] = _State()

    async def record_failure(self, engine: str, error: Exception | str = "") -> None:
        async with self._lock:
            state = self._states.setdefault(engine, _State())
            state.failures += 1
            state.last_error = str(error)
            if state.failures >= self.fail_threshold:
                state.blocked_until = time.monotonic() + self.cooldown_sec

    async def status(self, engine: str) -> dict:
        """Return diagnostic state suitable for tool output; never exposes request data."""
        async with self._lock:
            state = self._states.get(engine, _State())
            remaining = max(0.0, state.blocked_until - time.monotonic())
            return {
                "failures": state.failures,
                "cooldown_remaining_sec": round(remaining, 1),
                "available": remaining <= 0.0,
            }
