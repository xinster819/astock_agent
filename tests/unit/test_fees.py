"""core.fees · 交易成本。

费率是账本里唯一"外部世界定死"的常量，算错就是系统性偏差：
所有账户、所有轮次、所有对照实验一起错，而且错得完全一致——
不会有任何一条曲线看起来异常，因此不可能被事后察觉。
"""
import pytest

from astock.core import fees


class TestCommissionFloor:
    """佣金万 2.5，但单笔最低 5 元。保底是个阶梯，最容易被忽略。"""

    def test_small_trade_pays_the_floor(self):
        assert fees.commission(1_000.0) == 5.0

    def test_large_trade_pays_the_rate(self):
        assert fees.commission(100_000.0) == pytest.approx(25.0)

    def test_breakeven_point_is_20000(self):
        """2 万元成交额恰好是保底与比例的分界。"""
        assert fees.commission(20_000.0) == pytest.approx(5.0)
        assert fees.commission(20_001.0) > 5.0


class TestBuyFee:

    def test_has_no_stamp_tax(self):
        """印花税只在卖出单边收。"""
        amount = 100_000.0
        assert fees.buy_fee(amount) < fees.sell_fee(amount)

    def test_equals_commission_plus_transfer(self):
        amount = 100_000.0
        expected = fees.commission(amount) + amount * fees.TRANSFER_RATE
        assert fees.buy_fee(amount) == pytest.approx(round(expected, 2))


class TestSellFee:

    def test_includes_stamp_tax(self):
        amount = 100_000.0
        expected = (fees.commission(amount) + amount * fees.STAMP_TAX_RATE
                    + amount * fees.TRANSFER_RATE)
        assert fees.sell_fee(amount) == pytest.approx(round(expected, 2))

    def test_stamp_tax_dominates_large_trades(self):
        """千 1 的印花税是大额单的主要成本，别把它算漏。"""
        amount = 1_000_000.0
        assert fees.sell_fee(amount) - fees.buy_fee(amount) == pytest.approx(1000.0, abs=1.0)


class TestMaxAffordableQty:
    """⚠ 这个函数给出的是**乐观估计**：只扣比例费用，不含 5 元保底。

    调用方必须再用 buy_fee 复核一次——rules._affordable_qty 正是这么做的。
    这里把"乐观"这个性质本身钉下来，防止有人把它当成精确解直接用。
    """

    def test_rounds_down_to_lot(self):
        assert fees.max_affordable_qty(10_000.0, 10.0) % 100 == 0

    def test_returns_zero_for_nonpositive_inputs(self):
        assert fees.max_affordable_qty(0, 10.0) == 0
        assert fees.max_affordable_qty(10_000.0, 0) == 0
        assert fees.max_affordable_qty(-1, 10.0) == 0

    def test_estimate_can_exceed_real_affordability_on_small_trades(self):
        """记录已知的乐观偏差：994 元买 9.9 元的票，估计 100 股但实需 995.01。"""
        qty = fees.max_affordable_qty(994.0, 9.9)
        amount = round(9.9 * qty, 2)
        assert amount + fees.buy_fee(amount) > 994.0, "该边界正是逐手回退存在的理由"

    def test_estimate_is_accurate_for_large_trades(self):
        """大额单上保底佣金不再生效，估计值就是准的。"""
        qty = fees.max_affordable_qty(1_000_000.0, 10.0)
        amount = round(10.0 * qty, 2)
        assert amount + fees.buy_fee(amount) <= 1_000_000.0
