"""TDD tests for standalone deterministic portfolio risk controls."""
import unittest

from risk_guard import RiskGuard, classify_market_regime


class TestMarketRegime(unittest.TestCase):
    def test_normal_market(self):
        self.assertEqual(classify_market_regime(index_return=0.01, volatility=0.015, drawdown=-0.03), "normal")

    def test_high_volatility_market(self):
        self.assertEqual(classify_market_regime(index_return=0.01, volatility=0.03, drawdown=-0.03), "high_volatility")

    def test_risk_off_takes_precedence_over_high_volatility(self):
        # 阈值已校准为 -6%收益 / -15%回撤；此处用真正击穿新阈值的值验证优先级：
        # 即便波动率也很高，急跌市场也应判 risk_off 而非降级为 high_volatility。
        self.assertEqual(classify_market_regime(index_return=-0.07, volatility=0.05, drawdown=-0.10), "risk_off")
        self.assertEqual(classify_market_regime(index_return=0.0, volatility=0.03, drawdown=-0.16), "risk_off")

    def test_calibrated_thresholds_boundary(self):
        # 旧阈值(-3%/-7%)下会误判 risk_off 的"常态回调"，新阈值下应为 normal/high_vol。
        self.assertEqual(classify_market_regime(index_return=-0.04, volatility=0.01, drawdown=-0.08), "normal")
        # 恰好击穿新收益阈值 -6%。
        self.assertEqual(classify_market_regime(index_return=-0.06, volatility=0.01, drawdown=-0.05), "risk_off")
        # 恰好击穿新回撤阈值 -15%。
        self.assertEqual(classify_market_regime(index_return=0.0, volatility=0.01, drawdown=-0.15), "risk_off")


class TestRiskGuard(unittest.TestCase):
    def setUp(self):
        self.guard = RiskGuard(
            daily_loss_limit=0.05,
            max_drawdown=0.10,
            consecutive_loss_limit=2,
            loss_cooldown_trades=2,
            stop_loss_cooldown_trades=2,
        )

    def test_allows_healthy_portfolio(self):
        decision = self.guard.allow(equity=100_000, day_start_equity=100_000, peak_equity=105_000, symbol="AAA")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "ok")

    def test_daily_loss_circuit_breaker(self):
        decision = self.guard.allow(equity=94_999, day_start_equity=100_000, peak_equity=105_000)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "daily_loss_limit")

    def test_daily_loss_boundary_is_blocked(self):
        decision = self.guard.allow(equity=95_000, day_start_equity=100_000, peak_equity=105_000)
        self.assertFalse(decision.allowed)

    def test_max_drawdown(self):
        guard = RiskGuard(daily_loss_limit=0.10, max_drawdown=0.10)
        decision = guard.allow(equity=94_500, day_start_equity=100_000, peak_equity=105_000)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max_drawdown")

    def test_consecutive_losses_start_cooldown(self):
        self.guard.record_trade(-100, "AAA")
        self.guard.record_trade(-100, "BBB")
        blocked = self.guard.allow(100_000, 100_000, 100_000)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "consecutive_loss_cooldown")

    def test_loss_cooldown_expires_after_configured_trades(self):
        self.guard.record_trade(-100, "AAA")
        self.guard.record_trade(-100, "BBB")
        self.assertFalse(self.guard.allow(100_000, 100_000, 100_000).allowed)
        self.guard.record_trade(100, "CCC")
        self.assertFalse(self.guard.allow(100_000, 100_000, 100_000).allowed)
        self.guard.record_trade(100, "DDD")
        self.assertTrue(self.guard.allow(100_000, 100_000, 100_000).allowed)

    def test_profitable_trade_resets_consecutive_losses(self):
        self.guard.record_trade(-100, "AAA")
        self.guard.record_trade(100, "BBB")
        self.guard.record_trade(-100, "CCC")
        self.assertTrue(self.guard.allow(100_000, 100_000, 100_000).allowed)

    def test_symbol_stop_loss_cooldown(self):
        self.guard.record_trade(-100, "AAA", stop_loss=True)
        blocked = self.guard.allow(100_000, 100_000, 100_000, symbol="AAA")
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "symbol_stop_loss_cooldown")
        self.assertTrue(self.guard.allow(100_000, 100_000, 100_000, symbol="BBB").allowed)

    def test_symbol_stop_loss_cooldown_expires(self):
        self.guard.record_trade(-100, "AAA", stop_loss=True)
        self.guard.record_trade(100, "BBB")
        self.assertFalse(self.guard.allow(100_000, 100_000, 100_000, symbol="AAA").allowed)
        self.guard.record_trade(100, "CCC")
        self.assertTrue(self.guard.allow(100_000, 100_000, 100_000, symbol="AAA").allowed)

    def test_guard_state_round_trips(self):
        self.guard.record_trade(-100, "AAA", stop_loss=True)
        restored = RiskGuard(stop_loss_cooldown_trades=2)
        restored.restore(self.guard.to_dict())
        self.assertEqual(restored.allow(100_000, 100_000, 100_000, "AAA").reason,
                         "symbol_stop_loss_cooldown")

    def test_invalid_configuration_rejected(self):
        with self.assertRaises(ValueError):
            RiskGuard(daily_loss_limit=0)
        with self.assertRaises(ValueError):
            RiskGuard(max_drawdown=-0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
