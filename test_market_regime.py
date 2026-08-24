"""market_regime 集中化模块的行为测试：核心是"数据源失效时的处置分级"。"""
import os
import shutil
import tempfile
import unittest
import datetime as dt

import market
import market_regime as mr


class TestRegimeExceptionHandling(unittest.TestCase):
    """⚠ 测试隔离：本类会写/删 regime 缓存，必须重定向到临时文件。

    历史坑：test_cold_start 里 os.remove(mr.CACHE_FILE) 删的是【生产缓存】。
    正常情况下另两个用例会把它重新写回，但只要它们提前失败（例如环境缺 pandas），
    缓存就被永久删掉且无人察觉。而缓存一丢，classify() 退回 cold_start_default，
    多数实验组配置的 fallback 是 risk_off → effective_max_new=0 → 全组静默禁止开仓。
    又一次"静默失效"。现在统一重定向到 tempdir，测试绝不碰生产数据。
    """

    def setUp(self):
        self._orig = market.get_index_hist
        self._orig_cache = mr.CACHE_FILE
        self._tmpdir = tempfile.mkdtemp(prefix="regime_cache_test_")
        mr.CACHE_FILE = os.path.join(self._tmpdir, "market_regime_cache.json")

    def tearDown(self):
        market.get_index_hist = self._orig
        mr.CACHE_FILE = self._orig_cache
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_live_success_not_degraded(self):
        import pandas as pd
        # 造一段"明显 risk_off"的下跌序列
        closes = [100 - i * 0.5 for i in range(60)]
        dates = [(dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(60)]
        market.get_index_hist = lambda *a, **k: pd.DataFrame({"日期": dates, "收盘": closes})
        r = mr.classify()
        self.assertEqual(r.source, "live")
        self.assertFalse(r.degraded)
        self.assertEqual(r.regime, "risk_off")  # 持续下跌 → 真实 risk_off

    def test_source_down_falls_back_to_cache_not_blind_riskoff(self):
        import pandas as pd
        # 先成功一次写入 last-known-good = normal（平稳上行）
        closes = [100 + i * 0.05 for i in range(60)]
        dates = [(dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(60)]
        market.get_index_hist = lambda *a, **k: pd.DataFrame({"日期": dates, "收盘": closes})
        first = mr.classify()
        self.assertEqual(first.source, "live")
        self.assertEqual(first.regime, "normal")
        # 再让数据源全断 → 应回退 cache 的 normal，而非盲目 risk_off
        market.get_index_hist = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("断源"))
        second = mr.classify()
        self.assertEqual(second.source, "cache")
        self.assertTrue(second.degraded)
        self.assertEqual(second.regime, "normal")  # 关键：不是 risk_off

    def test_cold_start_uses_conservative_default_and_flags(self):
        # 删掉缓存模拟冷启动 + 断源（setUp 已把 CACHE_FILE 指向临时目录）
        try:
            os.remove(mr.CACHE_FILE)
        except OSError:
            pass
        market.get_index_hist = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("断源"))
        r = mr.classify(cold_start_default="risk_off")
        self.assertEqual(r.source, "cold_start_default")
        self.assertTrue(r.degraded)
        self.assertEqual(r.regime, "risk_off")


if __name__ == "__main__":
    unittest.main()
