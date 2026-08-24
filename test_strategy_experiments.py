"""Tests for the three orthogonal experimental signal families and risk exits."""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import strategy
from test_strategy_signals import _StrategyStub


def indicator(code, *, close=100.0, ma5=101.0, ma10=100.0, ma20=100.0,
              momentum=0.03, rsi14=50.0, volume_ratio=1.2,
              cross_up_ma20=True, below_ma10=False, golden_cross=False):
    return {
        "code": code, "close": close, "prev_close": close,
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "prev_ma20": ma20,
        "momentum": momentum, "rsi14": rsi14, "volume_ratio": volume_ratio,
        "cross_up_ma20": cross_up_ma20, "below_ma10": below_ma10,
        "golden_cross": golden_cross,
    }


class TestOrthogonalExperiments(unittest.TestCase):
    def _signals(self, ind, cfg, state=None):
        state = state or {"cash": 1_000_000.0, "init_cash": 1_000_000.0, "positions": {}}
        quotes = {code: {"code": code, "price": item["close"]} for code, item in ind.items()}
        with _StrategyStub(ind, list(ind)):
            return strategy.generate_signals(state, quotes, exp_config=cfg)

    def test_mean_reversion_buys_qualified_oversold(self):
        cfg = {"signal_logic": "mean_reversion", "rsi_buy_max": 35,
               "ma20_floor": 0.97, "momentum_threshold": -0.10,
               "max_positions": 3, "max_weight": 0.1, "max_new_per_round": 1}
        ind = {"MR": indicator("MR", close=98, ma5=99, ma10=100, ma20=100,
                                momentum=-0.04, rsi14=30, cross_up_ma20=False)}
        self.assertEqual([s["code"] for s in self._signals(ind, cfg) if s["action"] == "buy"], ["MR"])

    def test_mean_reversion_rejects_broken_midterm_trend(self):
        cfg = {"signal_logic": "mean_reversion", "rsi_buy_max": 35,
               "ma20_floor": 0.97, "momentum_threshold": -0.10,
               "max_positions": 3, "max_weight": 0.1, "max_new_per_round": 1}
        ind = {"MR": indicator("MR", close=95, ma20=100, momentum=-0.04,
                                rsi14=25, cross_up_ma20=False)}
        self.assertEqual([s for s in self._signals(ind, cfg) if s["action"] == "buy"], [])

    def test_quality_breakout_requires_volume_confirmation(self):
        cfg = {"signal_logic": "quality_breakout", "momentum_threshold": 0.03,
               "min_volume_ratio": 1.1, "max_positions": 3, "max_weight": 0.1,
               "max_new_per_round": 1}
        weak = {"QB": indicator("QB", close=102, ma20=100, momentum=0.04, volume_ratio=0.9)}
        strong = {"QB": indicator("QB", close=102, ma20=100, momentum=0.04, volume_ratio=1.2)}
        self.assertEqual([s for s in self._signals(weak, cfg) if s["action"] == "buy"], [])
        self.assertEqual([s["code"] for s in self._signals(strong, cfg) if s["action"] == "buy"], ["QB"])

    def test_risk_off_allows_exit_but_blocks_new_entry(self):
        cfg = {"signal_logic": "quality_breakout", "market_regime": "risk_off",
               "momentum_threshold": 0.03, "min_volume_ratio": 1.1,
               "max_positions": 3, "max_weight": 0.1, "max_new_per_round": 1}
        state = {"cash": 0.0, "init_cash": 1_000_000.0, "positions": {
            "OLD": {"qty": 100, "available": 100, "cost": 100.0, "name": "OLD"}}}
        ind = {
            "OLD": indicator("OLD", close=90, ma5=90, ma10=95, ma20=100, below_ma10=True),
            "NEW": indicator("NEW", close=102, ma20=100, momentum=0.04, volume_ratio=1.2),
        }
        signals = self._signals(ind, cfg, state)
        self.assertEqual([(s["action"], s["code"]) for s in signals], [("sell", "OLD")])

    def test_rebalance_sells_excess_weight(self):
        cfg = {"signal_logic": "quality_breakout", "market_regime": "risk_off",
               "max_positions": 3, "max_weight": 0.10, "max_new_per_round": 1}
        state = {"cash": 0.0, "init_cash": 1_000_000.0, "positions": {
            "HEAVY": {"qty": 2_000, "available": 2_000, "cost": 100.0, "name": "HEAVY"}}}
        ind = {"HEAVY": indicator("HEAVY", close=100, ma5=101, ma10=99, ma20=100,
                                    below_ma10=False, cross_up_ma20=False)}
        signals = self._signals(ind, cfg, state)
        self.assertEqual([(s["action"], s["code"], s["qty"]) for s in signals],
                         [("sell", "HEAVY", 1_800)])

    def test_rebalance_ignores_tiny_drift_within_deadband(self):
        """碎单空转 bug 回归：价格微涨使 allowed_qty 缩一手时，不得触发再平衡。

        持仓 8000 股、现价 12.52、组合 100 万、max_weight 0.10：
        allowed≈7987→7900，excess=100（恰一手）。这类由价格漂移产生的一手超额
        落在权重死区内，应被忽略——否则会出现"越涨越割一手"的负和碎单。
        """
        cfg = {"signal_logic": "cross_up_ma20", "market_regime": "risk_off",
               "max_positions": 3, "max_weight": 0.10, "max_new_per_round": 1}
        # cash 补足到使组合总额≈100 万，持仓恰约 10% 权重（真实 exp6/exp7 场景），
        # 而非把单票撑成 100% 权重的失真情形。
        state = {"cash": 899_840.0, "init_cash": 1_000_000.0, "positions": {
            "DRIFT": {"qty": 8_000, "available": 8_000, "cost": 12.38, "name": "DRIFT"}}}
        ind = {"DRIFT": indicator("DRIFT", close=12.52, ma5=12.6, ma10=12.4,
                                  ma20=12.3, below_ma10=False, cross_up_ma20=False)}
        signals = self._signals(ind, cfg, state)
        self.assertEqual([s for s in signals if s["action"] == "sell"], [])

    def test_rebalance_triggers_when_excess_exceeds_deadband(self):
        """真正超配（远超死区）时仍应一次性卖到合规位，不受死区影响。

        持仓 12000 股、现价 12.5、组合 100 万、max_weight 0.10：
        allowed=8000，excess=4000（=allowed 的 50%，远超 5% 死区），应整段卖出。
        """
        cfg = {"signal_logic": "cross_up_ma20", "market_regime": "risk_off",
               "max_positions": 3, "max_weight": 0.10, "max_new_per_round": 1}
        # 持仓市值 15 万 + 现金 85 万 = 组合 100 万；allowed=8000，excess=4000
        # （=allowed 的 50%，远超 5% 死区），应整段卖出。
        state = {"cash": 850_000.0, "init_cash": 1_000_000.0, "positions": {
            "OVER": {"qty": 12_000, "available": 12_000, "cost": 12.5, "name": "OVER"}}}
        ind = {"OVER": indicator("OVER", close=12.5, ma5=12.6, ma10=12.4,
                                 ma20=12.3, below_ma10=False, cross_up_ma20=False)}
        signals = self._signals(ind, cfg, state)
        self.assertEqual([(s["action"], s["code"], s["qty"]) for s in signals],
                         [("sell", "OVER", 4_000)])

    # ---- Batch 3: 放宽过严实验组（exp7 均值回归 / exp8 质量突破）----
    def test_mean_reversion_wider_floor_admits_deeper_pullback(self):
        """exp7 放宽 ma20_floor 0.97→0.93：更深回撤的超卖票应被纳入。

        RSI=30 超卖、close=94、ma20=100（close/ma20=0.94）：
        旧 floor 0.97 要求 close≥97 → 拒；新 floor 0.93 要求 close≥93 → 纳入买入。
        这直接破解"RSI≤35 与 close≥MA20×0.97 近乎互斥"导致的近死状态。
        """
        base = dict(signal_logic="mean_reversion", rsi_buy_max=35,
                    momentum_threshold=-0.10, max_positions=3, max_weight=0.10,
                    max_new_per_round=1, max_breakout_distance=0.03)
        ind = {"MR": indicator("MR", close=94, ma5=95, ma10=96, ma20=100,
                               momentum=-0.04, rsi14=30, cross_up_ma20=False)}
        strict = {**base, "ma20_floor": 0.97}
        loose = {**base, "ma20_floor": 0.93}
        self.assertEqual([s for s in self._signals(ind, strict) if s["action"] == "buy"], [])
        self.assertEqual([s["code"] for s in self._signals(ind, loose) if s["action"] == "buy"], ["MR"])

    def test_quality_breakout_relaxed_accepts_station_above_ma20(self):
        """exp8 放宽：站上 MA20（非当日上穿）+ 放量 + 动量，放宽档应买入，严格档不买。

        cross_up_ma20=False 但 close(102)≥ma20(100)、volume_ratio=1.2、momentum=0.04：
        - 严格档（无 breakout_relaxed）：强制当日上穿 → 不买（保持向后兼容）。
        - 放宽档（breakout_relaxed=True）：站上 MA20 即可 → 买入。
        """
        base = dict(signal_logic="quality_breakout", momentum_threshold=0.03,
                    min_volume_ratio=1.10, max_positions=3, max_weight=0.10,
                    max_new_per_round=1)
        ind = {"QB": indicator("QB", close=102, ma20=100, momentum=0.04,
                               volume_ratio=1.2, cross_up_ma20=False)}
        strict = {**base}
        relaxed = {**base, "breakout_relaxed": True}
        self.assertEqual([s for s in self._signals(ind, strict) if s["action"] == "buy"], [])
        self.assertEqual([s["code"] for s in self._signals(ind, relaxed) if s["action"] == "buy"], ["QB"])

    def test_quality_breakout_relaxed_still_requires_volume(self):
        """放宽同 bar 合取，但放量仍是硬门槛：弱量即便站上 MA20 也不买。"""
        cfg = dict(signal_logic="quality_breakout", momentum_threshold=0.03,
                   min_volume_ratio=1.10, max_positions=3, max_weight=0.10,
                   max_new_per_round=1, breakout_relaxed=True)
        weak = {"QB": indicator("QB", close=102, ma20=100, momentum=0.04,
                                volume_ratio=0.9, cross_up_ma20=False)}
        self.assertEqual([s for s in self._signals(weak, cfg) if s["action"] == "buy"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
