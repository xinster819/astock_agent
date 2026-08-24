"""执行层护栏 · 集成测试：互斥锁 + 冷却去抖真的拦住了重复触发。

【根因复盘】
一轮交易是"读 state → 生成信号 → 下单 → 写 state"。两个进程在数秒内同时启动
会都读到满血 state、各下一单、后写覆盖先写 —— trades 记两笔、state 只剩一笔，
即"幽灵成交"。exp4/exp5 曾出现间隔 6~8s 的重复成交，就是这个并发窗口。

两层防御：
  1) `trade.can_execute` 冷却去抖：距上次成功执行不足 60s 判为重复触发
  2) `trade.account_lock` 文件互斥：同一账户同时只允许一个执行者

【与重构前的区别】
旧版测试要 stub 掉 `run_exp._buy_exp` / `exp_manager.load_exp_state` 等一堆内部
函数才能跑——因为账本路径在 import 期就钉死了，碰不得真文件。而它 stub 的
`_buy_exp` 正是那份重复实现，测试因此并没有覆盖真正的下单路径。

现在账本路径是运行期解析的，conftest 给每个用例一个独立临时工作区，
于是这里可以跑**真实的 Account、真实的账本落盘**，只桩掉外网行情。
"""
import os
from datetime import datetime

import pytest

from astock.core.account import Account
from astock.guards import trade as tg
from astock.pipeline import round_engine
from astock.pipeline.round_engine import RoundPolicy, run_round

ACCOUNT = "exp1"
CODE = "600000"


@pytest.fixture
def offline_market(monkeypatch):
    """把行情层钉成确定值，让测试只考核护栏接线，不依赖外网。"""
    from astock.data import market
    from astock.strategy import signals

    monkeypatch.setattr(market, "is_trading_now", lambda now=None: (True, "交易中"))
    monkeypatch.setattr(market, "get_quotes", lambda codes: {
        c: {"code": c, "name": c, "price": 10.0, "limit_up": 11.0, "limit_down": 9.0}
        for c in codes})
    monkeypatch.setattr(market, "log_spread", lambda quotes: None)
    monkeypatch.setattr(market, "sample_spreads", lambda: (0, None))
    monkeypatch.setattr(signals, "load_pool", lambda: [CODE])
    # 市场状态走网络，这里固定为 normal
    monkeypatch.setattr(round_engine, "_current_regime", lambda config, out: "normal")


@pytest.fixture
def always_buy():
    """每轮都想买一手，用于观测护栏是否拦住了第二次。"""
    return lambda ctx: [{"action": "buy", "code": CODE, "qty": 100, "reason": "itest"}]


def _trade_count() -> int:
    return len(Account.open(ACCOUNT).ledger.read_trades())


def _run(decide, **kwargs):
    return run_round(ACCOUNT, decide, config={"name": "itest"},
                     policy=RoundPolicy(use_risk_guard=False),
                     force=True, verbose=False, **kwargs)


class TestCooldownDedup:
    """冷却去抖：数秒内的第二次触发不得再下单。"""

    def test_first_round_places_order(self, offline_market, always_buy):
        report = _run(always_buy)
        assert report.ordered is True
        assert len(report.fills) == 1, "首轮必须下单"
        assert "last_run_ts" in Account.open(ACCOUNT).state, "首轮须写入 last_run_ts"

    def test_second_rapid_call_skips_ordering(self, offline_market, always_buy):
        _run(always_buy)
        assert _trade_count() == 1

        # 模拟真实并发窗口：上一轮是 8 秒【之前】跑的
        acct = Account.open(ACCOUNT)
        acct.state["last_run_ts"] = datetime.now().timestamp() - 8
        acct.save()

        report = _run(always_buy)
        assert report.ordered is False, "冷却期内不得进入下单分支"
        assert report.skipped and "重复触发" in report.skipped
        assert _trade_count() == 1, "冷却期内第二次触发不得再产生成交"

    def test_after_cooldown_orders_again(self, offline_market, always_buy):
        _run(always_buy)
        acct = Account.open(ACCOUNT)
        acct.state["last_run_ts"] = datetime.now().timestamp() - 120   # 拨到冷却期外
        acct.save()

        report = _run(always_buy)
        assert report.ordered is True
        assert _trade_count() == 2, "过了冷却期应重新下单"

    def test_blocked_round_still_refreshes_equity(self, offline_market, always_buy):
        """被防抖拦下也要刷权益——权益曲线断点会被 freshness_gate 判为不新鲜。"""
        _run(always_buy)
        before = len(Account.open(ACCOUNT).ledger.read_equity())
        _run(always_buy)                       # 冷却期内
        assert len(Account.open(ACCOUNT).ledger.read_equity()) == before + 1

    def test_blocked_round_does_not_advance_last_run_ts(self, offline_market, always_buy):
        """被拦下时不得刷新时间戳，否则冷却窗口会被重复触发不断往后推。"""
        _run(always_buy)
        stamp = Account.open(ACCOUNT).state["last_run_ts"]
        _run(always_buy)
        assert Account.open(ACCOUNT).state["last_run_ts"] == stamp


class TestLockSerializesExecution:
    """账户锁被占用时应直接跳过：不抛异常、不下单、不损坏账本。"""

    def test_busy_lock_skips(self, offline_market, always_buy):
        with tg.account_lock(ACCOUNT):
            report = _run(always_buy)
        assert report.ordered is False
        assert report.skipped and "占用" in report.skipped
        assert not os.path.exists(Account.open(ACCOUNT).paths.trades), "被锁时不得产生成交"

    def test_lock_released_after_round(self, offline_market, always_buy):
        _run(always_buy)
        # 锁在轮次结束后必须释放，否则下一轮会被自己挡在门外
        with tg.account_lock(ACCOUNT):
            pass


class TestClockSkewIsSelfHealing:
    """`last_run_ts` 落在未来时（时钟回拨/坏写入）不冻结账户。

    这是刻意的取舍：把未来时间戳一律判为"冷却中"会让账户永久停摆，
    而停摆恰恰是本项目最忌讳的静默失效。放行并覆写时间戳能自愈，
    代价是极端时钟异常下可能漏掉一次去抖——两害相权取其轻。
    """

    def test_future_timestamp_does_not_freeze_account(self, offline_market, always_buy):
        acct = Account.open(ACCOUNT)
        acct.state["last_run_ts"] = datetime.now().timestamp() + 3600
        acct.save()

        report = _run(always_buy)
        assert report.ordered is True, "未来时间戳不应让账户永久停摆"
        assert Account.open(ACCOUNT).state["last_run_ts"] < datetime.now().timestamp() + 1
