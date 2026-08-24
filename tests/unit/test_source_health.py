"""数据源熔断 + 轮内指标缓存的测试。

两者都只影响【延迟】，不影响任何判定口径——这正是需要被测试钉住的边界：
熔断掉的源在结果里仍以 error 出现，交叉验证阈值一字未改。
"""
import unittest

from astock.data import source_health
from astock.strategy import signals as strategy


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestCircuitBreaker(unittest.TestCase):

    def setUp(self):
        self.clock = _Clock()
        self.h = source_health.SourceHealth(fail_threshold=3, cooldown_sec=300,
                                            clock=self.clock)

    def test_healthy_source_is_never_skipped(self):
        for _ in range(10):
            self.h.record_ok("sina")
        self.assertFalse(self.h.should_skip("sina"))

    def test_unknown_source_is_not_skipped(self):
        self.assertFalse(self.h.should_skip("never_seen"))

    def test_below_threshold_does_not_trip(self):
        """容忍偶发抖动：连挂两次还不熔断。"""
        self.h.record_fail("eastmoney")
        self.h.record_fail("eastmoney")
        self.assertFalse(self.h.should_skip("eastmoney"))

    def test_trips_at_threshold(self):
        for _ in range(3):
            self.h.record_fail("eastmoney")
        self.assertTrue(self.h.should_skip("eastmoney"))

    def test_success_resets_the_counter(self):
        self.h.record_fail("eastmoney")
        self.h.record_fail("eastmoney")
        self.h.record_ok("eastmoney")
        self.h.record_fail("eastmoney")
        self.assertFalse(self.h.should_skip("eastmoney"))

    def test_cooldown_expiry_allows_one_probe(self):
        for _ in range(3):
            self.h.record_fail("eastmoney")
        self.assertTrue(self.h.should_skip("eastmoney"))
        self.clock.advance(301)
        self.assertFalse(self.h.should_skip("eastmoney"), "冷却期满应放行一次探测")

    def test_failed_probe_reopens_immediately(self):
        """探测再失败必须立刻重新熔断，不能又白等 3 次。"""
        for _ in range(3):
            self.h.record_fail("eastmoney")
        self.clock.advance(301)
        self.h.should_skip("eastmoney")          # 放行探测
        self.h.record_fail("eastmoney")          # 探测失败
        self.assertTrue(self.h.should_skip("eastmoney"))

    def test_successful_probe_fully_recovers(self):
        for _ in range(3):
            self.h.record_fail("eastmoney")
        self.clock.advance(301)
        self.h.should_skip("eastmoney")
        self.h.record_ok("eastmoney")
        self.assertFalse(self.h.should_skip("eastmoney"))
        self.clock.advance(10_000)
        self.assertFalse(self.h.should_skip("eastmoney"))

    def test_sources_are_independent(self):
        for _ in range(3):
            self.h.record_fail("eastmoney")
        self.assertTrue(self.h.should_skip("eastmoney"))
        self.assertFalse(self.h.should_skip("sina"))
        self.assertFalse(self.h.should_skip("tencent"))

    def test_state_snapshot_is_observable(self):
        for _ in range(3):
            self.h.record_fail("eastmoney")
        st = self.h.state()
        self.assertEqual(st["eastmoney"]["fails"], 3)
        self.assertGreater(st["eastmoney"]["open_for"], 0)

    def test_rejects_invalid_config(self):
        with self.assertRaises(ValueError):
            source_health.SourceHealth(fail_threshold=0)
        with self.assertRaises(ValueError):
            source_health.SourceHealth(cooldown_sec=-1)


class TestFetchAllStillReportsSkippedSourceAsError(unittest.TestCase):
    """熔断的源必须仍以 error 形式出现，交叉验证才会照旧把它算作"无效源"。"""

    def setUp(self):
        source_health.QUOTES.reset()

    def tearDown(self):
        source_health.QUOTES.reset()

    def test_circuit_open_source_appears_as_error(self):
        from astock.data import quote_sources as qs
        real = dict(qs.SOURCES)
        try:
            qs.SOURCES.clear()
            qs.SOURCES["boom"] = lambda code: (_ for _ in ()).throw(RuntimeError("down"))
            for _ in range(source_health.DEFAULT_FAIL_THRESHOLD):
                qs.fetch_all("600519", retries=1)
            result = qs.fetch_all("600519", retries=1)
            self.assertIn("error", result["boom"])
            self.assertTrue(result["boom"].get("circuit_open"))
        finally:
            qs.SOURCES.clear()
            qs.SOURCES.update(real)


class TestIndicatorCache(unittest.TestCase):

    def setUp(self):
        strategy.clear_indicator_cache()
        self.calls = []
        self._real = strategy._compute_indicators
        strategy._compute_indicators = self._counting

    def tearDown(self):
        strategy._compute_indicators = self._real
        strategy.clear_indicator_cache()

    def _counting(self, code):
        self.calls.append(code)
        return {"code": code, "close": 10.0, "ma20": 9.0}

    def test_repeated_calls_hit_cache(self):
        for _ in range(5):
            strategy._indicators("600519")
        self.assertEqual(self.calls, ["600519"])

    def test_different_codes_are_cached_separately(self):
        strategy._indicators("600519")
        strategy._indicators("000001")
        strategy._indicators("600519")
        self.assertEqual(self.calls, ["600519", "000001"])

    def test_clear_forces_refetch_next_round(self):
        """跨轮必须重新取数，否则日线永远停在第一轮。"""
        strategy._indicators("600519")
        strategy.clear_indicator_cache()
        strategy._indicators("600519")
        self.assertEqual(self.calls, ["600519", "600519"])

    def test_none_result_is_also_cached(self):
        """取数失败也缓存，避免一轮内对同一只票反复重试网络。"""
        strategy._compute_indicators = lambda code: self.calls.append(code) or None
        self.assertIsNone(strategy._indicators("999999"))
        self.assertIsNone(strategy._indicators("999999"))
        self.assertEqual(self.calls, ["999999"])


if __name__ == "__main__":
    unittest.main()
