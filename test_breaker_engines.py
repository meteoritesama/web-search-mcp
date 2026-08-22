"""Regression tests for search cache and per-engine circuit breaking."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import engines
from breaker import EngineCircuitBreaker
from rank import extract_core_entities, score_result


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


class RankRegressionTests(unittest.TestCase):
    def test_entity_gate_penalizes_no_core_entity_evidence(self):
        self.assertEqual(extract_core_entities("2026 国考 98:1"), ["2026", "国考", "98:1"])
        score = score_result("2026 国考 98:1", "无关日历", "节假日安排")
        self.assertLess(score, 0.01)

    def test_source_confirmed_year_hook_is_optional(self):
        baseline = score_result("2026 国考", "2026 国考公告", "计划招录")
        penalized = score_result("2026 国考", "2026 国考公告", "计划招录", real_year="2024", query_year="2026")
        self.assertAlmostEqual(penalized, baseline * 0.1)


class SearchQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_low_quality_results_continue_to_fallback(self):
        weak = [{"title": "calendar", "url": "https://example.test", "snippet": "calendar", "relevance": 0.1}]
        strong = [{"title": "2026年 节假日 安排", "url": "https://gov.cn/test", "snippet": "2026年节假日安排", "relevance": 0.8}]
        breaker = MagicMock()
        breaker.allow = AsyncMock(return_value=True)
        breaker.record_success = AsyncMock()
        config = {
            **engines.CONFIG, "engine_fallback": True, "engine_breaker_enabled": True,
            "relevance_filter": False, "relevance_keep_min": 1,
            "relevance_quality_min_avg": 0.15, "adaptive_retrieval_enabled": False,
        }
        with patch.object(engines, "CONFIG", config), \
              patch.object(engines, "BREAKER", breaker), \
              patch.object(engines, "_engine_once", AsyncMock(side_effect=[(weak, 0, 1, False), (strong, 0, 1, False)])), \
              patch.object(engines, "_compensate_snippet", AsyncMock(side_effect=lambda r, q: r)):
            response = await engines.search("2026年 节假日 安排", engine="bing", max_results=3)

        self.assertEqual(response["engine"], "baidu")
        self.assertIn("bing_quality_fail", response["engine_errors"])

    async def test_search_multi_penalizes_non_authoritative_bing_singleton(self):
        result = [{"title": "2026 国考", "url": "https://example.test/a", "snippet": "2026 国考"}]
        config = {**engines.CONFIG, "engine_breaker_enabled": False, "relevance_filter": True}
        with patch.object(engines, "CONFIG", config), \
              patch.object(engines, "_engine_results", AsyncMock(return_value=(result, True))), \
              patch.object(engines, "_compensate_snippet", AsyncMock(side_effect=lambda r, q: r)):
            response = await engines.search_multi("2026 国考", engines=["bing"], max_results=1)

        self.assertLessEqual(response["results"][0]["relevance"], 0.4)


if __name__ == "__main__":
    unittest.main()
