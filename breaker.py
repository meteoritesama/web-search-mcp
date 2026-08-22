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
        # The breaker is instantiated at module import time by engines.py.  Create the
        # lock only from an active event loop and replace it if a restarted server uses
        # this process-local breaker from a different loop.
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def allow(self, engine: str) -> bool:
        """Return False while an engine is in its cooldown window."""
        async with self._get_lock():
            state = self._states.get(engine)
            if not state:
                return True
            return time.monotonic() >= state.blocked_until

    async def record_success(self, engine: str) -> None:
        async with self._get_lock():
            self._states[engine] = _State()

    async def record_failure(self, engine: str, error: Exception | str = "") -> None:
        async with self._get_lock():
            state = self._states.setdefault(engine, _State())
            state.failures += 1
            state.last_error = str(error)
            # Requests already in flight may fail after another request has opened the
            # circuit.  Count those failures for diagnostics, but do not extend the
            # existing cooldown window indefinitely.
            if state.failures >= self.fail_threshold and time.monotonic() >= state.blocked_until:
                state.blocked_until = time.monotonic() + self.cooldown_sec

    async def status(self, engine: str) -> dict:
        """Return diagnostic state suitable for tool output; never exposes request data."""
        async with self._get_lock():
            state = self._states.get(engine, _State())
            remaining = max(0.0, state.blocked_until - time.monotonic())
            return {
                "failures": state.failures,
                "cooldown_remaining_sec": round(remaining, 1),
                "available": remaining <= 0.0,
            }
