"""Regression tests for search cache and per-engine circuit breaking."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import engines
from breaker import EngineCircuitBreaker


class EngineCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_is_created_lazily_inside_event_loop(self):
        breaker = EngineCircuitBreaker()
        self.assertIsNone(breaker._lock)

        self.assertTrue(await breaker.allow("baidu"))
        self.assertIsNotNone(breaker._lock)

    async def test_failures_during_cooldown_do_not_extend_cooldown(self):
        breaker = EngineCircuitBreaker(fail_threshold=2, cooldown_sec=60)
        with patch("breaker.time.monotonic", return_value=100.0):
            await breaker.record_failure("baidu", "first")
            await breaker.record_failure("baidu", "opens circuit")
        self.assertEqual(breaker._states["baidu"].blocked_until, 160.0)

        with patch("breaker.time.monotonic", return_value=120.0):
            await breaker.record_failure("baidu", "in-flight failure")
        self.assertEqual(breaker._states["baidu"].blocked_until, 160.0)
        self.assertEqual(breaker._states["baidu"].failures, 3)


class EngineCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_results_marks_cache_hits(self):
        config = {**engines.CONFIG, "search_cache_ttl_sec": 60}
        with patch.object(engines, "CONFIG", config), \
             patch.object(engines._cache, "get", return_value=[{"title": "cached"}]), \
             patch.object(engines._cache, "put") as put, \
             patch.dict(engines.ENGINES, {"bing": AsyncMock()}):
            results, from_cache = await engines._engine_results("bing", "query", 5)

        self.assertTrue(from_cache)
        self.assertEqual(results, [{"title": "cached"}])
        put.assert_not_called()

    async def test_search_does_not_reset_breaker_for_cache_hit(self):
        cached_results = [{"title": "cached", "url": "https://example.test", "snippet": "cached"}]
        breaker = MagicMock()
        breaker.allow = AsyncMock(return_value=True)
        breaker.record_success = AsyncMock()

        config = {
            **engines.CONFIG,
            "engine_fallback": False,
            "engine_breaker_enabled": True,
            "relevance_filter": False,
        }

        with patch.object(engines, "CONFIG", config), \
             patch.object(engines, "BREAKER", breaker), \
             patch.object(engines, "_engine_results", AsyncMock(return_value=(cached_results, True))):
            response = await engines.search("query", engine="bing", max_results=5)

        self.assertEqual(response["results"], cached_results)
        breaker.record_success.assert_not_awaited()

    async def test_search_resets_breaker_after_real_request(self):
        results = [{"title": "fresh", "url": "https://example.test", "snippet": "fresh"}]
        breaker = MagicMock()
        breaker.allow = AsyncMock(return_value=True)
        breaker.record_success = AsyncMock()

        config = {
            **engines.CONFIG,
            "engine_fallback": False,
            "engine_breaker_enabled": True,
            "relevance_filter": False,
        }

        with patch.object(engines, "CONFIG", config), \
             patch.object(engines, "BREAKER", breaker), \
             patch.object(engines, "_engine_results", AsyncMock(return_value=(results, False))):
            await engines.search("query", engine="bing", max_results=5)

        breaker.record_success.assert_awaited_once_with("bing")

    async def test_search_multi_does_not_reset_breaker_for_cache_hit(self):
        cached_results = [{"title": "cached", "url": "https://example.test", "snippet": "cached"}]
        breaker = MagicMock()
        breaker.allow = AsyncMock(return_value=True)
        breaker.record_success = AsyncMock()

        config = {**engines.CONFIG, "engine_breaker_enabled": True, "relevance_filter": False}

        with patch.object(engines, "CONFIG", config), \
             patch.object(engines, "BREAKER", breaker), \
             patch.object(engines, "_engine_results", AsyncMock(return_value=(cached_results, True))):
            response = await engines.search_multi("query", engines=["bing"], max_results=5)

        self.assertEqual(response["count"], 1)
        breaker.record_success.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
