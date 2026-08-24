"""ops.stall_check · 引擎停摆自检。

这段逻辑原先是 scheduler_tick.sh 里的一段内联 Python heredoc——没有测试、
没有静态检查、重构时也搜不到。本次分包就把它里面的 `import market_time`
打断了，而它恰恰是用来发现"东西悄悄坏了"的那段代码。
**一个自己会静默失效的停摆检测器，比没有更糟。**

搬进代码库之后，它终于有了下面这些用例。
"""
import datetime as dt

from astock.core.account import Account
from astock.ops import stall_check

NOW = dt.datetime(2026, 8, 25, 15, 0)


def _account(account_id, **state_overrides):
    account = Account.open(account_id)
    account.state.update(state_overrides)
    account.save()
    return account


class TestFindStalled:

    def test_skips_accounts_without_a_ledger(self, isolated_env):
        """尚未开张 ≠ 停摆。新账户不该被报成故障。"""
        assert stall_check.find_stalled(now=NOW) == []

    def test_healthy_account_is_not_flagged(self, isolated_env):
        _account("exp1", round=42,
                 last_trading_round_date=NOW.strftime("%Y-%m-%d"))
        assert "exp1" not in stall_check.find_stalled(now=NOW)

    def test_account_that_never_entered_the_ordering_branch_is_flagged(self, isolated_env):
        """事故现场：进程在跑、权益照写，但 last_trading_round_date 停在三周前。"""
        _account("exp1", round=10,
                 last_trading_round_date=(NOW - dt.timedelta(days=21)).strftime("%Y-%m-%d"))
        assert "exp1" in stall_check.find_stalled(now=NOW)

    def test_flags_each_stalled_account_separately(self, isolated_env):
        stale = (NOW - dt.timedelta(days=21)).strftime("%Y-%m-%d")
        fresh = NOW.strftime("%Y-%m-%d")
        _account("exp1", round=10, last_trading_round_date=stale)
        _account("exp2", round=10, last_trading_round_date=stale)
        _account("exp3", round=10, last_trading_round_date=fresh)

        stalled = stall_check.find_stalled(now=NOW)
        assert "exp1" in stalled and "exp2" in stalled
        assert "exp3" not in stalled

    def test_zero_trades_alone_is_not_stall(self, isolated_env):
        """高门槛策略长期无信号是正常的。判据必须是"是否进下单分支"。"""
        account = _account("exp7", round=50,
                           last_trading_round_date=NOW.strftime("%Y-%m-%d"))
        assert account.ledger.read_trades() == [], "本用例前提：确实一笔成交都没有"
        assert "exp7" not in stall_check.find_stalled(now=NOW)


class TestReport:

    def test_reports_all_clear(self, isolated_env):
        lines = []
        assert stall_check.report(printer=lines.append) == 0
        assert "✅" in lines[0]

    def test_names_the_stalled_accounts_loudly(self, isolated_env):
        """宁可吵，也不沉默——停摆必须点名，不能只给个数字。"""
        _account("exp1", round=10,
                 last_trading_round_date="2026-07-31")
        lines = []
        count = stall_check.report(printer=lines.append)
        assert count == 1
        assert "🔴" in lines[0] and "exp1" in lines[0]
        assert any("进程时区" in line for line in lines), "要给出排查方向"


class TestSchedulerContract:
    """调度脚本依赖的契约：报告即目的，发现停摆不该让整个 tick 失败。"""

    def test_cli_returns_zero_even_when_stalled(self, isolated_env, capsys):
        from astock.cli.main import main

        _account("exp1", round=10, last_trading_round_date="2026-07-31")
        assert main(["stall-check"]) == 0
        assert "停摆账户" in capsys.readouterr().out
