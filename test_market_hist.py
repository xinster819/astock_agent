"""个股日线多源兜底 + 交易所代码映射的回归测试（全部离线，不发网络请求）。

两个真实的坑：
  1) 东财 push2/push2his 会整站 502（2026-08-23 实测）。get_hist 原先只有这一条路，
     一挂则 strategy._indicators() 全返回 None → 所有策略静默不开仓且无告警。
  2) 个股代码不能用 _index_symbol 映射：000001 作为个股是平安银行(sz000001)，
     而 sh000001 是上证指数。用错会让整条日线序列静默变成指数行情——
     价格量级完全不同，却不会抛任何异常。
"""
import json
import unittest

import market


class TestSymbolMapping(unittest.TestCase):
    """个股映射与指数映射必须分开，000001 是这两者的分水岭。"""

    def test_shanghai_stocks(self):
        for code in ("600519", "601899", "603259", "688981"):
            self.assertEqual(market._stock_symbol(code), "sh" + code)

    def test_shenzhen_stocks(self):
        for code in ("000001", "000858", "002460", "300750"):
            self.assertEqual(market._stock_symbol(code), "sz" + code)

    def test_000001_stock_is_ping_an_bank_not_the_index(self):
        """分水岭用例：个股 000001 必须是 sz（平安银行），不是 sh（上证指数）。"""
        self.assertEqual(market._stock_symbol("000001"), "sz000001")
        self.assertEqual(market._index_symbol("000001"), "sh000001")
        self.assertNotEqual(market._stock_symbol("000001"),
                            market._index_symbol("000001"))

    def test_already_prefixed_is_passed_through(self):
        self.assertEqual(market._stock_symbol("sz000001"), "sz000001")
        self.assertEqual(market._stock_symbol("sh600519"), "sh600519")


class _FakeAk:
    """替身 akshare。可分别控制两个接口的成败。"""

    def __init__(self, hist_ok=False, daily_ok=False):
        self.hist_ok, self.daily_ok = hist_ok, daily_ok
        self.daily_symbol_seen = None

    def stock_zh_a_hist(self, **kw):
        if not self.hist_ok:
            raise ConnectionError("Remote end closed connection without response")
        import pandas as pd
        return pd.DataFrame({"日期": ["2026-08-21"], "收盘": [1.0], "成交量": [1.0]})

    def stock_zh_a_daily(self, symbol=None, adjust=None):
        self.daily_symbol_seen = symbol
        if not self.daily_ok:
            raise RuntimeError("sina down")
        import pandas as pd
        return pd.DataFrame({
            "date": ["2026-08-20", "2026-08-21"],
            "open": [10.0, 11.0], "high": [12.0, 12.5],
            "low": [9.5, 10.5], "close": [11.0, 11.41],
            "volume": [100.0, 200.0],
        })


class _HistFallbackCase(unittest.TestCase):
    def setUp(self):
        self._real_http = market._http_get
        self._modules = __import__("sys").modules
        self._saved_ak = self._modules.get("akshare")

    def tearDown(self):
        market._http_get = self._real_http
        if self._saved_ak is None:
            self._modules.pop("akshare", None)
        else:
            self._modules["akshare"] = self._saved_ak

    def install_ak(self, fake):
        self._modules["akshare"] = fake


class TestGetHistFallback(_HistFallbackCase):

    def test_eastmoney_used_when_healthy(self):
        self.install_ak(_FakeAk(hist_ok=True))
        df = market.get_hist("600519", "20260801", "20260823")
        self.assertEqual(len(df), 1)

    def test_falls_back_to_sina_when_eastmoney_502(self):
        fake = _FakeAk(hist_ok=False, daily_ok=True)
        self.install_ak(fake)
        df = market.get_hist("000001", "20260820", "20260821")
        self.assertEqual(list(df.columns),
                         ["日期", "开盘", "收盘", "最高", "最低", "成交量"])
        self.assertEqual(float(df["收盘"].iloc[-1]), 11.41)

    def test_sina_fallback_uses_stock_symbol_not_index_symbol(self):
        """回归：000001 必须以 sz000001 去取，否则拿到的是上证指数。"""
        fake = _FakeAk(hist_ok=False, daily_ok=True)
        self.install_ak(fake)
        market.get_hist("000001", "20260820", "20260821")
        self.assertEqual(fake.daily_symbol_seen, "sz000001")

    def test_falls_back_to_tencent_when_akshare_all_down(self):
        self.install_ak(_FakeAk(hist_ok=False, daily_ok=False))
        payload = {"data": {"sz000001": {"qfqday": [
            ["2026-08-20", "10.0", "11.0", "12.0", "9.5", "100"],
            ["2026-08-21", "11.0", "11.41", "12.5", "10.5", "200"],
        ]}}}
        market._http_get = lambda url, timeout=8: json.dumps(payload)
        df = market.get_hist("000001", "20260820", "20260821")
        self.assertEqual(float(df["收盘"].iloc[-1]), 11.41)
        self.assertEqual(float(df["成交量"].iloc[-1]), 200.0)

    def test_date_range_is_clipped(self):
        self.install_ak(_FakeAk(hist_ok=False, daily_ok=True))
        df = market.get_hist("000001", "20260821", "20260821")
        self.assertEqual(len(df), 1)
        self.assertEqual(df["日期"].iloc[0], "2026-08-21")

    def test_raises_with_all_source_errors_when_everything_fails(self):
        self.install_ak(_FakeAk(hist_ok=False, daily_ok=False))
        def boom(url, timeout=8):
            raise OSError("tencent down")
        market._http_get = boom
        with self.assertRaises(RuntimeError) as ctx:
            market.get_hist("000001", "20260801", "20260823")
        msg = str(ctx.exception)
        # 失败必须点名每一路，不能静默
        for token in ("em:", "sina:", "tencent:"):
            self.assertIn(token, msg)


class TestIndicatorsSurviveEastmoneyOutage(_HistFallbackCase):
    """端到端：东财挂了，策略指标仍须算得出来（否则全组静默不开仓）。"""

    def test_indicators_computed_from_fallback_source(self):
        import strategy
        fake = _FakeAk(hist_ok=False, daily_ok=False)
        self.install_ak(fake)
        closes = [10 + i * 0.1 for i in range(40)]
        rows = [[f"2026-07-{d + 1:02d}" if d < 31 else f"2026-08-{d - 30:02d}",
                 f"{c:.2f}", f"{c:.2f}", f"{c + 0.2:.2f}", f"{c - 0.2:.2f}", "1000"]
                for d, c in enumerate(closes)]
        payload = {"data": {"sz000001": {"qfqday": rows}}}
        market._http_get = lambda url, timeout=8: json.dumps(payload)
        ind = strategy._indicators("000001")
        self.assertIsNotNone(ind, "东财故障时指标不应为 None——那会让策略静默不开仓")
        self.assertAlmostEqual(ind["close"], closes[-1], places=2)
        self.assertIsNotNone(ind["ma20"])
        self.assertIsNotNone(ind["volume_ratio"])


if __name__ == "__main__":
    unittest.main()
