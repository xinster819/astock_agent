"""
strategy 信号逻辑的 TDD 测试 —— 修「假金叉」。
=====================================================================
根因：exp4 配置名为「金叉策略 / MA5上穿MA20金叉」，但 strategy.py 的
ma5_cross_ma20 分支只判「当前 ma5 > ma20」（已多头），根本没检测“上穿”这个
穿越事件（上一周期 ma5<=ma20 且当前 ma5>ma20）。后果：任何早已多头、
冲高很久的票都会触发买入 → 追高接盘（exp4 买北方华创@831、动量41% 即此）。

被测契约（修复后）：
  strategy.generate_signals(st, quotes, exp_config={"signal_logic":"ma5_cross_ma20",...})
  只有在「上一周期 MA5<=MA20 且当前 MA5>MA20」的真金叉才产出 buy 信号；
  对「已多头但非穿越」的票不得买入。

为隔离信号逻辑，_indicators 被打桩：注入预先构造好的 MA/动量与
新增的 golden_cross 事件标记。golden_cross 由 strategy 依据历史序列判定，
本测试通过打桩 _indicators 的返回来断言 generate_signals 的取舍。
"""
import unittest
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from astock.strategy import signals as strategy


def _ind(code, ma5, ma20, momentum, golden_cross, close=10.0):
    """构造一个 _indicators 返回值。"""
    return {
        "code": code, "close": close, "prev_close": close,
        "ma5": ma5, "ma10": ma5, "ma20": ma20, "prev_ma20": ma20,
        "momentum": momentum,
        "cross_up_ma20": False, "below_ma10": False,
        "golden_cross": golden_cross,   # 新契约：MA5 上穿 MA20 的穿越事件
    }


class _StrategyStub:
    """打桩 strategy._indicators / load_pool / market_value，纯内存。"""

    def __init__(self, indicators_by_code, pool):
        self.map = indicators_by_code
        self.pool = pool
        self._orig = {}

    def __enter__(self):
        self._orig["_indicators"] = strategy._indicators
        self._orig["load_pool"] = strategy.load_pool
        strategy._indicators = lambda code: self.map.get(code)
        strategy.load_pool = lambda: self.pool
        # market_value 被 from broker import market_value 动态引用，打桩到 broker
        from astock.core import broker
        self._orig["mv"] = broker.market_value
        broker.market_value = lambda st, quotes: (
            sum(
                float(p.get("qty", 0)) * float((quotes.get(code) or {}).get("price") or p.get("cost", 0))
                for code, p in st.get("positions", {}).items()
            ),
            float(st["cash"]) + sum(
                float(p.get("qty", 0)) * float((quotes.get(code) or {}).get("price") or p.get("cost", 0))
                for code, p in st.get("positions", {}).items()
            ),
        )
        return self

    def __exit__(self, *a):
        strategy._indicators = self._orig["_indicators"]
        strategy.load_pool = self._orig["load_pool"]
        from astock.core import broker
        broker.market_value = self._orig["mv"]


CFG = {"signal_logic": "ma5_cross_ma20", "momentum_threshold": 0.0,
       "max_positions": 5, "max_new_per_round": 2, "max_weight": 0.20}


class TestGoldenCrossIsRealCross(unittest.TestCase):

    def _run(self, indicators, quotes, cfg=None):
        st = {"cash": 1_000_000.0, "init_cash": 1_000_000.0, "positions": {}}
        pool = list(indicators.keys())
        with _StrategyStub(indicators, pool):
            return strategy.generate_signals(st, quotes, exp_config=cfg or CFG)

    def test_real_golden_cross_buys(self):
        # 真金叉：MA5 刚上穿 MA20 → 应买入
        inds = {"AAA": _ind("AAA", ma5=10.5, ma20=10.0, momentum=0.05, golden_cross=True)}
        quotes = {"AAA": {"code": "AAA", "price": 10.0}}
        sigs = self._run(inds, quotes)
        buys = [s for s in sigs if s["action"] == "buy"]
        self.assertEqual(len(buys), 1, "真金叉必须买入")
        self.assertEqual(buys[0]["code"], "AAA")

    def test_risk_off_blocks_new_buys(self):
        inds = {"RISK": _ind("RISK", ma5=10.5, ma20=10.0, momentum=0.05, golden_cross=True)}
        quotes = {"RISK": {"code": "RISK", "price": 10.0}}
        sigs = self._run(
            inds, quotes, cfg=dict(CFG, market_regime="risk_off")
        )
        self.assertEqual([s for s in sigs if s["action"] == "buy"], [])

    def test_already_bullish_not_bought(self):
        # 已多头但非穿越：ma5>ma20 却 golden_cross=False → 不得买（旧假金叉会误买）
        inds = {"BBB": _ind("BBB", ma5=12.0, ma20=10.0, momentum=0.40, golden_cross=False)}
        quotes = {"BBB": {"code": "BBB", "price": 10.0}}
        sigs = self._run(inds, quotes)
        buys = [s for s in sigs if s["action"] == "buy"]
        self.assertEqual(buys, [], "已多头非穿越（追高）必须被拒，这正是 exp4 的病根")

    def test_cross_but_weak_momentum_rejected(self):
        # 金叉但动量不达标 → 仍拒（阈值仍然生效）
        inds = {"CCC": _ind("CCC", ma5=10.1, ma20=10.0, momentum=-0.05, golden_cross=True)}
        quotes = {"CCC": {"code": "CCC", "price": 10.0}}
        cfg = dict(CFG, momentum_threshold=0.0)
        st = {"cash": 1_000_000.0, "init_cash": 1_000_000.0, "positions": {}}
        with _StrategyStub(inds, ["CCC"]):
            sigs = strategy.generate_signals(st, quotes, exp_config=cfg)
        self.assertEqual([s for s in sigs if s["action"] == "buy"], [],
                         "金叉但动量<阈值仍不买")


class TestIndicatorsExposesGoldenCross(unittest.TestCase):
    """_indicators 必须提供 golden_cross 字段（真穿越判定），供 generate_signals 使用。"""

    def test_last_bar_cross_true(self):
        # [100]*21 + [103] 恰好末根 MA5 上穿 MA20
        closes = [100.0] * 21 + [103.0]
        self.assertTrue(strategy._golden_cross(closes),
                        "末根发生 MA5 上穿 MA20 应判 True")

    def test_prev_bar_not_cross(self):
        closes = [100.0] * 21          # 尚未拉升
        self.assertFalse(strategy._golden_cross(closes))

    def test_already_bullish_not_cross(self):
        # 持续上涨、早已多头 → 非穿越
        closes = [80.0 + i * 2 for i in range(25)]
        self.assertFalse(strategy._golden_cross(closes),
                         "已多头（非穿越）不得判为金叉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
