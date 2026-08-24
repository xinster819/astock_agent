"""core.account · 账户门面：规则 + 账本的组合。

`Account` 取代了两套并行的账本读写（`broker.*_state` 与 `exp_manager.*_exp_state`）。
本文件重点验证**合并后行为一致**，以及账户之间是否真的互不干扰——
13 个账户互为对照，一次串账就毁掉全部结论。
"""
import pytest

from astock.core.account import Account
from astock.core.fees import INIT_CASH
from astock.runtime import clock


class TestOpen:

    def test_initializes_on_first_open(self, isolated_env):
        account = Account.open("exp1", init_cash=500_000.0)
        assert account.state["cash"] == 500_000.0
        assert account.state["init_cash"] == 500_000.0
        assert account.state["round"] == 0
        assert account.paths.state.exists(), "首次打开就该落盘，别等到第一次 save"

    def test_defaults_to_standard_init_cash(self, isolated_env):
        assert Account.open("A").state["cash"] == INIT_CASH

    def test_reopen_reads_existing_state(self, isolated_env):
        first = Account.open("exp1")
        first.state["cash"] = 12_345.0
        first.save()
        assert Account.open("exp1").state["cash"] == 12_345.0

    def test_init_cash_ignored_once_ledger_exists(self, isolated_env):
        """已开张的账户不能被 init_cash 重置——那等于凭空印钱。"""
        Account.open("exp1", init_cash=100.0)
        assert Account.open("exp1", init_cash=999_999.0).state["cash"] == 100.0

    def test_dates_come_from_exchange_clock(self, isolated_env):
        """账本日期一律走交易所时钟，不用进程本地时间。

        2026-07-31 停摆事故的根因就是二者不一致。
        """
        state = Account.initial_state()
        assert state["created"] == clock.today()
        assert state["last_settle_date"] == clock.today()

    def test_extra_fields_are_merged(self, isolated_env):
        account = Account.open("exp1", extra={"exp_id": "exp1"})
        assert account.state["exp_id"] == "exp1"


class TestIsolation:
    """账户隔离是整个对照实验的地基。"""

    def test_accounts_do_not_share_state(self, isolated_env, make_quote):
        a, b = Account.open("A"), Account.open("exp1")
        a.buy(make_quote("600519", price=10.0), 1000)
        a.save()
        assert Account.open("exp1").state["cash"] == INIT_CASH
        assert Account.open("A").state["cash"] < INIT_CASH
        assert b.state["cash"] == INIT_CASH

    def test_thirteen_accounts_open_simultaneously(self, isolated_env, make_quote):
        from astock.runtime import paths

        accounts = [Account.open(p.account) for p in paths.all_accounts()]
        assert len(accounts) == 13
        accounts[0].buy(make_quote("600519", price=10.0), 100)
        accounts[0].save()
        for other in accounts[1:]:
            assert other.state["cash"] == INIT_CASH, f"{other.account_id} 被串账了"

    def test_ledgers_are_written_to_separate_files(self, isolated_env, make_quote):
        for account_id in ("A", "B", "exp1"):
            account = Account.open(account_id)
            account.buy(make_quote("600519", price=10.0), 100)
            account.save()
        for account_id in ("A", "B", "exp1"):
            assert len(Account.open(account_id).ledger.read_trades()) == 1


class TestTrading:

    def test_successful_fill_is_logged(self, account, make_quote):
        result = account.buy(make_quote("600519", price=10.0), 1000)
        assert result.ok
        trades = account.ledger.read_trades()
        assert len(trades) == 1
        assert trades[0]["方向"] == "买入"
        assert trades[0]["数量"] == "1000"

    def test_rejected_order_leaves_no_trace(self, account, make_quote):
        """trades.csv 是成交流水，不是尝试日志。拒单不该留痕。"""
        result = account.buy(make_quote("600519", price=0), 1000)
        assert not result.ok
        assert account.ledger.read_trades() == []

    def test_quote_timestamp_is_used_when_present(self, account, make_quote):
        account.buy(make_quote("600519", price=10.0, ts="2026-08-25 10:30:00"), 100)
        assert account.ledger.read_trades()[0]["时间"] == "2026-08-25 10:30:00"

    def test_settle_new_day_is_idempotent(self, account, make_quote):
        account.buy(make_quote("600519", price=10.0), 1000)
        assert account.state["positions"]["600519"]["available"] == 0, "T+1 当日冻结"
        account.state["last_settle_date"] = "2026-01-01"      # 假装跨了一天
        assert account.settle_new_day() is True
        assert account.state["positions"]["600519"]["available"] == 1000
        assert account.settle_new_day() is False, "同一天内不得重复结算"


class TestEquitySnapshot:

    def test_writes_a_row(self, account, make_quote):
        total, ret = account.snapshot_equity({})
        assert total == pytest.approx(INIT_CASH)
        assert ret == 0.0
        assert len(account.ledger.read_equity()) == 1

    def test_write_false_skips_the_row(self, account):
        account.snapshot_equity({}, write=False)
        assert account.ledger.read_equity() == []

    def test_peak_equity_advances_even_without_writing(self, account, make_quote):
        """最大回撤风控依赖 peak_equity；漏更新会让回撤显得比实际小，闸门失灵。"""
        account.buy(make_quote("600519", price=10.0), 1000)
        account.snapshot_equity({"600519": make_quote("600519", price=50.0)}, write=False)
        peak = account.state["peak_equity"]
        assert peak > INIT_CASH
        account.snapshot_equity({"600519": make_quote("600519", price=1.0)}, write=False)
        assert account.state["peak_equity"] == peak, "峰值只升不降"


class TestReload:

    def test_reload_picks_up_other_writers(self, isolated_env):
        """持锁后必须 reload，才能看到上一执行者落盘的 last_run_ts。"""
        first = Account.open("exp1")
        second = Account.open("exp1")
        second.state["last_run_ts"] = 12345.0
        second.save()
        assert "last_run_ts" not in first.state
        first.reload()
        assert first.state["last_run_ts"] == 12345.0
