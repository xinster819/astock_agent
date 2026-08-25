"""core.rules · A 股撮合规则。

【为什么这个文件很重要】
重构前，`broker.py` 是这个仓库里**唯一动钱的模块，测试覆盖率是 0**。
不是没人想测，是测不了：buy/sell 一边算撮合、一边往固定路径写 CSV、
一边依赖 import 期读环境变量定下的模块级全局路径。想断言一条规则，
得先准备文件系统、设环境变量、再 reload 模块。

规则拆成纯函数之后，下面每个用例都只是"给个 state 和 quote，看账户变成什么样"。

被测契约：`buy/sell(state, quote, qty, reason) -> Execution`
  成功时就地修改 state 并返回带 Fill 的 Execution；失败时 state 必须原封不动。
"""
import pytest

from astock.core import fees, rules

INIT = 1_000_000.0


@pytest.fixture
def state():
    return {"cash": INIT, "init_cash": INIT, "positions": {}}


@pytest.fixture
def held(state, make_quote):
    """已持有 1000 股、且已过 T+1 解冻的账户。"""
    rules.buy(state, make_quote("600519", price=10.0), 1000)
    state["positions"]["600519"]["available"] = 1000
    return state


# =========================================================== 买入

class TestBuyRejections:
    """拒单路径。每一条拒绝都必须**不动账户**——半成交比不成交危险得多。"""

    def test_zero_price_is_refused(self, state, make_quote):
        """price=0 是多源交叉验证判定"脏价"后的信号，绝不能成交。"""
        result = rules.buy(state, make_quote("600519", price=0), 100)
        assert not result.ok
        assert "无有效现价" in result.message
        assert state["cash"] == INIT and not state["positions"]

    def test_limit_up_board_is_refused(self, state, make_quote):
        """封涨停买不进——真实市场里挂单排不上。"""
        quote = make_quote("600519", price=11.0, limit_up=11.0)
        result = rules.buy(state, quote, 100)
        assert not result.ok
        assert "涨停" in result.message
        assert state["cash"] == INIT

    def test_below_one_lot_is_refused(self, state, make_quote):
        result = rules.buy(state, make_quote("600519", price=10.0), 99)
        assert not result.ok
        assert "不足一手" in result.message

    def test_insufficient_cash_is_refused(self, make_quote):
        state = {"cash": 100.0, "init_cash": 100.0, "positions": {}}
        result = rules.buy(state, make_quote("600519", price=10.0), 100)
        assert not result.ok
        assert state["cash"] == 100.0


class TestBuyRounding:

    def test_quantity_rounds_down_to_lot(self, state, make_quote):
        """A 股买入必须整手，多出的零股向下抹掉而不是拒单。"""
        result = rules.buy(state, make_quote("600519", price=10.0), 250)
        assert result.ok
        assert result.fill.qty == 200

    def test_shrinks_to_affordable_lots(self, make_quote):
        """现金不够时缩量到买得起的最大整手，而不是一律拒单。

        真实下单同样是"能买多少买多少"；一律拒单会让满仓附近的策略行为失真。
        """
        state = {"cash": 5_000.0, "init_cash": 5_000.0, "positions": {}}
        result = rules.buy(state, make_quote("600519", price=10.0), 1000)
        assert result.ok
        assert result.fill.qty == 400          # 5000 元最多买 400 股（含费）
        assert state["cash"] >= 0


class TestBuyNeverOverdrawsCash:
    """回归：可买数量的解析解忽略了 5 元佣金保底，边界上会让现金穿负。

    旧实现：现金 994、价 9.9 —— 解析解算出 100 股，实需 990 + 5.01 = 995.01，
    落账现金 -1.01。而 integrity_gate 的"负现金"红旗正是为这类 bug 准备的，
    等于系统自己会举报自己，却没人先把 bug 修掉。
    """

    @pytest.mark.parametrize("cash,price", [
        (994.0, 9.9),      # 原始复现场景
        (995.0, 9.9),
        (1000.0, 9.9),
        (500.0, 4.9),      # 小额单：保底佣金占比更高
        (2000.0, 19.99),
    ])
    def test_cash_never_goes_negative(self, cash, price, make_quote):
        state = {"cash": cash, "init_cash": cash, "positions": {}}
        rules.buy(state, make_quote("600519", price=price), 10_000)
        assert state["cash"] >= 0, f"现金穿负：{state['cash']}"

    def test_boundary_case_refuses_rather_than_overdraw(self, make_quote):
        state = {"cash": 994.0, "init_cash": 994.0, "positions": {}}
        result = rules.buy(state, make_quote("600519", price=9.9), 200)
        assert not result.ok
        assert state["cash"] == 994.0


class TestBuyAccounting:

    def test_cost_basis_includes_fees(self, state, make_quote):
        """含费成本价：买入费摊进成本，卖出时算出的已实现盈亏才是真实到手数。"""
        rules.buy(state, make_quote("600519", price=10.0), 1000)
        position = state["positions"]["600519"]
        amount, fee = 10_000.0, fees.buy_fee(10_000.0)
        assert position["cost"] == pytest.approx((amount + fee) / 1000, abs=1e-4)
        assert position["cost"] > 10.0, "成本价必须高于成交价——费用要摊进去"

    def test_cash_decreases_by_amount_plus_fee(self, state, make_quote):
        rules.buy(state, make_quote("600519", price=10.0), 1000)
        assert state["cash"] == pytest.approx(INIT - 10_000.0 - fees.buy_fee(10_000.0))

    def test_second_buy_averages_cost(self, state, make_quote):
        rules.buy(state, make_quote("600519", price=10.0), 1000)
        rules.buy(state, make_quote("600519", price=20.0), 1000)
        position = state["positions"]["600519"]
        assert position["qty"] == 2000
        assert 15.0 < position["cost"] < 15.1     # 加权均价 + 摊进的费用

    def test_t_plus_one_freezes_new_shares(self, state, make_quote):
        """当日买入不计入可用数量——这是 T+1 的全部含义。"""
        rules.buy(state, make_quote("600519", price=10.0), 1000)
        position = state["positions"]["600519"]
        assert position["qty"] == 1000
        assert position["available"] == 0

    def test_records_opened_at_for_time_stop(self, state, make_quote):
        """建仓时刻是时间止损的唯一判据。

        重构前只有 run_exp 那份复制的买入逻辑写这个字段，broker.buy 不写，
        于是 A/B/C/D 组的 time_stop_days 配置项存在但永不触发。
        """
        rules.buy(state, make_quote("600519", price=10.0), 1000)
        assert state["positions"]["600519"]["opened_at"]


# =========================================================== 卖出

class TestSellRejections:

    def test_no_position_is_refused(self, state, make_quote):
        result = rules.sell(state, make_quote("600519", price=10.0), 100)
        assert not result.ok
        assert "无持仓" in result.message

    def test_frozen_shares_cannot_be_sold(self, state, make_quote):
        """当日买入当日不可卖。"""
        rules.buy(state, make_quote("600519", price=10.0), 1000)
        cash_after_buy = state["cash"]
        result = rules.sell(state, make_quote("600519", price=11.0), 1000)
        assert not result.ok
        assert "T+1" in result.message
        assert state["cash"] == cash_after_buy

    def test_limit_down_board_is_refused(self, held, make_quote):
        """封跌停卖不出。"""
        quote = make_quote("600519", price=9.0, limit_down=9.0)
        result = rules.sell(held, quote, 1000)
        assert not result.ok
        assert "跌停" in result.message

    def test_zero_price_is_refused(self, held, make_quote):
        result = rules.sell(held, make_quote("600519", price=0), 1000)
        assert not result.ok
        assert held["positions"]["600519"]["qty"] == 1000


class TestSellAccounting:

    def test_realized_pnl_is_net_of_all_fees(self, held, make_quote):
        result = rules.sell(held, make_quote("600519", price=20.0), 1000)
        assert result.ok
        amount = 20_000.0
        # 含费成本 = 买入成交额 + 买入费；卖出盈亏还要再扣一次卖出费
        cost_basis = 10_000.0 + fees.buy_fee(10_000.0)
        expected = round(amount - fees.sell_fee(amount) - cost_basis, 2)
        assert result.fill.realized_pnl == pytest.approx(expected, abs=0.01)

    def test_full_exit_removes_position(self, held, make_quote):
        rules.sell(held, make_quote("600519", price=11.0), 1000)
        assert "600519" not in held["positions"]

    def test_partial_sell_keeps_position_and_lot_rounding(self, held, make_quote):
        """非清仓的卖出必须整手。"""
        result = rules.sell(held, make_quote("600519", price=11.0), 550)
        assert result.ok
        assert result.fill.qty == 500
        assert held["positions"]["600519"]["qty"] == 500
        assert held["positions"]["600519"]["available"] == 500

    def test_odd_lot_full_exit_is_allowed(self, state, make_quote):
        """零股只能一次性清仓卖出——非清仓时的整手约束不适用。"""
        rules.buy(state, make_quote("600519", price=10.0), 100)
        state["positions"]["600519"]["qty"] = 50      # 模拟送股产生的零股
        state["positions"]["600519"]["available"] = 50
        result = rules.sell(state, make_quote("600519", price=11.0), 50)
        assert result.ok
        assert result.fill.qty == 50

    def test_sell_quantity_capped_by_available(self, held, make_quote):
        held["positions"]["600519"]["available"] = 300
        result = rules.sell(held, make_quote("600519", price=11.0), 1000)
        assert result.fill.qty == 300


# =========================================================== 跨日结算与估值

class TestSettlement:

    def test_unfreezes_shares_on_new_day(self, state, make_quote):
        rules.buy(state, make_quote("600519", price=10.0), 1000)
        assert rules.settle_new_day(state, "2026-08-25") is True
        assert state["positions"]["600519"]["available"] == 1000

    def test_is_idempotent_within_the_same_day(self, state):
        state["last_settle_date"] = "2026-08-25"
        assert rules.settle_new_day(state, "2026-08-25") is False


class TestValuation:

    def test_market_value_uses_live_price(self, held, make_quote):
        mv, total = rules.market_value(held, {"600519": make_quote("600519", price=20.0)})
        assert mv == 20_000.0
        assert total == pytest.approx(held["cash"] + 20_000.0)

    def test_falls_back_to_cost_when_price_unavailable(self, held):
        """取不到价按成本估值——保守，绝不用陈旧价格制造浮盈浮亏。"""
        mv, _ = rules.market_value(held, {"600519": {"code": "600519", "price": 0}})
        assert mv == pytest.approx(held["positions"]["600519"]["cost"] * 1000)

    def test_return_pct_is_relative_to_init_cash(self):
        assert rules.total_return_pct({"init_cash": 1000.0}, 1100.0) == 10.0

    def test_return_pct_survives_zero_init_cash(self):
        """除零不该把报告整个搞崩。"""
        assert rules.total_return_pct({"init_cash": 0}, 100.0) == 0.0
