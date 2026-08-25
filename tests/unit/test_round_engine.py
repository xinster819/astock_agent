"""pipeline.round_engine · 唯一的一份轮次编排。

这份实现取代了 run.py / run_exp.py / execute.py 里三份逐字复制的编排。
本文件的重点不是"跑通"，而是把那三份实现**发散出来的差异**钉住，
防止它们以别的形式重新长回来。
"""
import pytest

from astock.core.account import Account
from astock.core.fees import INIT_CASH
from astock.guards import trade as trade_guard
from astock.pipeline import round_engine
from astock.pipeline.round_engine import OrderPlan, RoundPolicy, run_round

CODE = "600000"


@pytest.fixture
def offline(monkeypatch):
    """把所有联网点钉成确定值，只留编排逻辑受测。"""
    from astock.data import market
    from astock.strategy import signals

    monkeypatch.setattr(market, "is_trading_now", lambda now=None: (True, "交易中"))
    monkeypatch.setattr(market, "get_quotes", lambda codes: {
        c: {"code": c, "name": c, "price": 10.0, "limit_up": 11.0, "limit_down": 9.0}
        for c in codes})
    monkeypatch.setattr(market, "log_spread", lambda quotes: None)
    monkeypatch.setattr(market, "sample_spreads", lambda: (0, None))
    monkeypatch.setattr(signals, "load_pool", lambda: [CODE])
    monkeypatch.setattr(round_engine, "_current_regime", lambda config, out: "normal")


@pytest.fixture
def closed_market(offline, monkeypatch):
    from astock.data import market
    monkeypatch.setattr(market, "is_trading_now", lambda now=None: (False, "休市"))


def buy_one(ctx):
    return [{"action": "buy", "code": CODE, "qty": 100, "reason": "test"}]


def _run(decide=buy_one, account="exp1", **kwargs):
    kwargs.setdefault("policy", RoundPolicy(use_risk_guard=False))
    kwargs.setdefault("force", True)
    kwargs.setdefault("verbose", False)
    return run_round(account, decide, config={"name": "t"}, **kwargs)


class TestHappyPath:

    def test_places_orders_and_records_round(self, offline):
        report = _run()
        assert report.ordered is True
        assert len(report.fills) == 1
        state = Account.open("exp1").state
        assert state["round"] == 1
        assert state["cash"] < INIT_CASH

    def test_writes_last_trading_round_date(self, offline):
        """`freshness_gate.stalled_engine` 靠这个字段发现"进程在跑但从未下单"。

        判据必须是"是否进入下单分支"而不是"是否有成交"——
        高门槛策略长期无信号是正常的，长期不进下单分支才是故障。
        """
        _run()
        assert Account.open("exp1").state["last_trading_round_date"]

    def test_empty_decision_still_advances_the_round(self, offline):
        """没有指令 ≠ 引擎没转。轮次照样要推进，否则会被误判为停摆。"""
        report = _run(decide=lambda ctx: [])
        assert report.ordered is True
        assert report.fills == []
        assert Account.open("exp1").state["round"] == 1

    def test_writes_equity_snapshot(self, offline):
        _run()
        assert len(Account.open("exp1").ledger.read_equity()) == 1


class TestSellsExecuteBeforeBuys:
    """先卖后买：卖出腾出的现金本轮即可用于买入。"""

    def test_sell_precedes_buy_regardless_of_input_order(self, offline, make_quote):
        account = Account.open("exp1")
        account.buy(make_quote(CODE, price=10.0), 1000)
        account.state["positions"][CODE]["available"] = 1000
        account.save()

        report = _run(decide=lambda ctx: [
            {"action": "buy", "code": CODE, "qty": 100, "reason": "b"},
            {"action": "sell", "code": CODE, "qty": 1000, "reason": "s"},
        ])
        assert [f.side for f in report.fills] == ["卖出", "买入"]


class TestNonTradingHours:

    def test_skips_ordering_but_refreshes_equity(self, closed_market):
        report = _run(force=False)
        assert report.ordered is False
        assert report.fills == []
        assert len(Account.open("exp1").ledger.read_equity()) == 1

    def test_force_overrides_session_check_only(self, closed_market):
        """--force 只放行时段判断，其余硬校验一律照旧。"""
        report = _run(force=True)
        assert report.ordered is True
        assert len(report.fills) == 1

    def test_force_does_not_bypass_limit_up(self, closed_market, monkeypatch):
        from astock.data import market
        monkeypatch.setattr(market, "get_quotes", lambda codes: {
            c: {"code": c, "name": c, "price": 11.0, "limit_up": 11.0} for c in codes})
        report = _run(force=True)
        assert report.fills == []


class TestGuardPolicy:
    """A 组是对照基线：统一代码路径，但不能悄悄给它加上组合风控。"""

    def test_control_group_policy_disables_risk_guard(self):
        assert RoundPolicy.control_group().use_risk_guard is False

    def test_control_group_still_gets_lock_and_cooldown(self):
        """互斥锁与冷却只拦重复触发，不改策略语义，所有账户都该有。"""
        policy = RoundPolicy.control_group()
        assert policy.cooldown_sec == trade_guard.DEFAULT_COOLDOWN_SEC

    def test_risk_guard_state_persisted_only_when_enabled(self, offline):
        _run(policy=RoundPolicy(use_risk_guard=False))
        assert "risk_guard" not in Account.open("exp1").state
        _run(account="exp2", policy=RoundPolicy(use_risk_guard=True, cooldown_sec=None))
        assert "risk_guard" in Account.open("exp2").state

    def test_day_start_equity_tracked_even_without_risk_guard(self, offline):
        """基准照常维护——报表要用，且日后打开风控时基准是连续的。"""
        _run(policy=RoundPolicy(use_risk_guard=False))
        state = Account.open("exp1").state
        assert "day_start_equity" in state and "peak_equity" in state

    def test_cooldown_none_disables_debounce(self, offline):
        _run(policy=RoundPolicy(use_risk_guard=False, cooldown_sec=None))
        report = _run(policy=RoundPolicy(use_risk_guard=False, cooldown_sec=None))
        assert report.ordered is True, "cooldown_sec=None 应完全关闭防抖"


class TestOrderPlan:

    def test_plain_list_is_accepted(self):
        plan = OrderPlan.of([{"action": "buy"}])
        assert plan.max_new_buys is None and len(plan.orders) == 1

    def test_max_new_buys_counts_successful_fills_only(self, offline, monkeypatch):
        """额度约束的是**成交**笔数：前面的单被拒了，后面的候选要能补位。

        这正是它不能在 decider 里自行截断的原因——decider 拿不到成交结果。
        """
        from astock.data import market
        monkeypatch.setattr(market, "get_quotes", lambda codes: {
            "600000": {"code": "600000", "name": "涨停", "price": 11.0, "limit_up": 11.0},
            "600001": {"code": "600001", "name": "正常", "price": 10.0, "limit_up": 11.0},
        })
        report = _run(decide=lambda ctx: OrderPlan(orders=[
            {"action": "buy", "code": "600000", "qty": 100, "reason": "会被涨停拒"},
            {"action": "buy", "code": "600001", "qty": 100, "reason": "应当补位成交"},
        ], max_new_buys=1))
        assert [f.code for f in report.fills] == ["600001"]

    def test_max_new_buys_zero_blocks_all_entries(self, offline):
        """risk_off 下不开新仓。"""
        report = _run(decide=lambda ctx: OrderPlan(orders=buy_one(None), max_new_buys=0))
        assert report.fills == []


class TestMissingQuote:

    def test_order_without_quote_is_skipped_loudly(self, offline):
        report = _run(decide=lambda ctx: [
            {"action": "buy", "code": "999999", "qty": 100, "reason": "无行情"}])
        assert report.fills == []
        assert any("无行情快照" in line for line in report.lines)


class TestRegimePersistence:

    def test_regime_is_written_to_state(self, offline):
        _run()
        assert Account.open("exp1").state["market_regime"] == "normal"


class TestRuleAccountEntrypoints:
    """`run_rule` 是规则组的薄壳：A 组与 exp 组的差异只在参数上。"""

    @pytest.fixture(autouse=True)
    def stub_indicators(self, monkeypatch):
        """这两个用例跑的是真 `rule_decider`，它会去取日线算指标——桩掉。

        本组考核的是"参数怎么传"，不是"信号怎么算"（后者见 test_signal_families）。
        """
        from astock.strategy import signals

        monkeypatch.setattr(signals, "_indicators", lambda code: None)

    def test_control_round_uses_the_control_policy(self, offline, monkeypatch):
        from astock.pipeline import run_rule

        captured = {}
        real = round_engine.run_round

        def spy(account_id, decide, **kwargs):
            captured.update(account_id=account_id, **kwargs)
            return real(account_id, decide, **kwargs)

        monkeypatch.setattr(run_rule, "run_round", spy)
        run_rule.run_control(force=True, verbose=False)

        assert captured["account_id"] == "A"
        assert captured["policy"].use_risk_guard is False, "对照基线不加组合风控"
        assert captured["policy"].cooldown_sec is not None, "但仍要有防抖"

    def test_experiment_round_passes_its_config(self, offline, monkeypatch):
        from astock.pipeline import run_rule

        captured = {}
        real = round_engine.run_round

        def spy(account_id, decide, **kwargs):
            captured.update(account_id=account_id, **kwargs)
            return real(account_id, decide, **kwargs)

        monkeypatch.setattr(run_rule, "run_round", spy)
        run_rule.run_experiment("exp4", force=True, verbose=False)

        assert captured["account_id"] == "exp4"
        assert captured["config"]["signal_logic"] == "ma5_cross_ma20"
        assert captured.get("policy") is None, "实验组用默认 policy（含风控）"

    def test_unknown_experiment_returns_none_without_crashing(self, offline):
        from astock.pipeline import run_rule

        assert run_rule.run_experiment("exp99", verbose=False) is None
