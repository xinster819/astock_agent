"""market_regime 集中化模块的行为测试：核心是"数据源失效时的处置分级"。"""
import datetime as dt
import pathlib
import unittest

import pytest

from astock.data import market
from astock.guards import regime as mr


@pytest.mark.usefixtures("isolated_env")
class TestRegimeExceptionHandling(unittest.TestCase):
    """数据源失效时 classify() 的三级处置：live → 缓存 → 冷启动默认。

    历史坑：这里原先要手工把 mr.CACHE_FILE 重定向到 tempdir，否则
    test_cold_start 里的 os.remove 删的是【生产缓存】。缓存一丢，classify()
    退回 cold_start_default，多数实验组的 fallback 是 risk_off →
    effective_max_new=0 → 全组静默禁止开仓，又一次"静默失效"。

    重构后缓存路径由 runtime.paths 在运行期解析，conftest 的 isolated_env
    给每个用例一个独立工作区——手工重定向连同它的注意事项一起不需要了。
    """

    def setUp(self):
        self._orig = market.get_index_hist

    def tearDown(self):
        market.get_index_hist = self._orig

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
        # 删掉缓存模拟冷启动 + 断源（工作区已由 isolated_env 隔离到 tmp）
        pathlib.Path(mr._cache_file()).unlink(missing_ok=True)
        market.get_index_hist = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("断源"))
        r = mr.classify(cold_start_default="risk_off")
        self.assertEqual(r.source, "cold_start_default")
        self.assertTrue(r.degraded)
        self.assertEqual(r.regime, "risk_off")


if __name__ == "__main__":
    unittest.main()
