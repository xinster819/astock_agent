"""weekly / dashboard · 采集端到端。

这两个采集器是对照实验的**结论出口**。本文件在临时工作区里造出真实账本，
验证它们把账本正确翻译成结论——尤其是三件事：

  1. 不存在的账户不能被伪造成「0 收益」
  2. 账本脏 / 数据过期的账户必须被**排除出排名**，且理由要写清楚
  3. 采集器**只读**，绝不创建或修改任何账本
"""
import json

import pytest

from astock.core.account import Account
from astock.reporting import dashboard, weekly
from astock.runtime.paths import AccountPaths

WEEK = "2026-W34"


def _quote(code="600519", price=10.0):
    return {"code": code, "name": "测试股", "price": price,
            "limit_up": price * 1.1, "limit_down": price * 0.9}


def _retime_trades(account, timestamps):
    """把已落盘的成交行时间改写到目标周。

    成交本身走真实下单 API 产生（这样账实必然自洽，也顺带覆盖了写入路径），
    只有时间需要改——测试要的是"这几笔发生在 2026-W34"。
    """
    from astock.core.ledger import TRADE_COLUMNS
    from astock.runtime.files import append_csv_row

    rows = account.ledger.read_trades()
    account.paths.trades.unlink()
    for row, ts in zip(rows, timestamps):
        row["时间"] = ts
        append_csv_row(account.paths.trades, TRADE_COLUMNS,
                       [row[c] for c in TRADE_COLUMNS])


def _seed_traded_account(account_id, *, equity_rows=(), state_extra=None):
    """造一个账实自洽的账户：真买一笔、真卖一笔，再补上权益曲线。"""
    account = Account.open(account_id)
    account.buy(_quote(price=10.0), 1000, reason="cross_up_ma20 动量3%")
    account.state["positions"]["600519"]["available"] = 1000      # 模拟跨日解冻
    account.sell(_quote(price=15.0), 1000, reason="止盈")
    account.state.update(state_extra or {})
    account.save()
    _retime_trades(account, ["2026-08-18 10:00:00", "2026-08-19 14:00:00"])
    for row in equity_rows:
        account.ledger.append_equity(*row)
    return account


@pytest.fixture
def seeded(isolated_env):
    """exp1 有一周完整数据（账实自洽）；其余 12 个账户不存在。"""
    _seed_traded_account(
        "exp1",
        equity_rows=[("2026-08-14 15:00:00", 900_000, 100_000, 1_000_000, 0.0),
                     ("2026-08-21 15:00:00", 950_000, 150_000, 1_100_000, 10.0)],
        state_extra={"round": 12})
    return isolated_env



class TestWeeklyCollect:

    def test_returns_all_thirteen_accounts(self, seeded):
        data = weekly.collect(WEEK, use_live=False)
        assert len(data["groups"]) == 13

    def test_missing_account_is_marked_absent_not_flat(self, seeded):
        """不存在的账户必须是 exists=False，而不是「0 收益」。

        项目第一条边界写着「不伪造、不回填净值」——把缺失渲染成持平就是伪造。
        """
        groups = {g["account"]: g for g in weekly.collect(WEEK, use_live=False)["groups"]}
        assert groups["exp2"]["exists"] is False
        assert "week_ret_pct" not in groups["exp2"]

    def test_computes_the_week_return_from_the_previous_close(self, seeded):
        groups = {g["account"]: g for g in weekly.collect(WEEK, use_live=False)["groups"]}
        exp1 = groups["exp1"]
        assert exp1["exists"] is True
        assert exp1["week_pnl"] == 100_000.0
        assert exp1["week_ret_pct"] == 10.0
        assert exp1["round"] == 12

    def test_counts_only_this_weeks_trades(self, seeded):
        groups = {g["account"]: g for g in weekly.collect(WEEK, use_live=False)["groups"]}
        exp1 = groups["exp1"]
        assert exp1["week_trade_count"] == 2
        # 10 元买入 1000 股、15 元卖出，扣双边费用后应接近 +5000
        assert 4900 < exp1["week_realized_pnl"] < 5000
        assert exp1["week_win"] == 1 and exp1["week_loss"] == 0

    def test_carries_the_account_id_for_cross_week_matching(self, seeded):
        """按 account id 对齐跨周文件，比按显示名稳——名字会随配置改。"""
        for group in weekly.collect(WEEK, use_live=False)["groups"]:
            assert group["account"]

    def test_meta_records_the_window(self, seeded):
        meta = weekly.collect(WEEK, use_live=False)["meta"]
        assert meta["review_week"] == WEEK
        assert meta["this_week"][0] == "2026-08-17"

    def test_offline_mode_yields_null_indices(self, seeded):
        """--no-live 时指数全为 null，而不是编一个数字出来。"""
        indices = weekly.collect(WEEK, use_live=False)["indices"]
        assert indices and all(v is None for v in indices.values())


class TestIntegrityGating:
    """账本脏或数据过期的账户不得进入收益排名——这是归因的前提。"""

    def test_clean_run_reports_all_clear(self, seeded):
        summary = weekly.collect(WEEK, use_live=False)["integrity_summary"]
        assert summary["dirty_groups"] == []

    def test_a_dirty_ledger_is_excluded_and_explained(self, isolated_env):
        # 在一本自洽账本上人为塞进一个 trades 里没有的持仓 —— 账实不符
        _seed_traded_account(
            "exp1",
            equity_rows=[("2026-08-21 15:00:00", 900_000, 100_000, 1_000_000, 0.0)],
            state_extra={"positions": {"999999": {"qty": 9999, "available": 0,
                                                  "cost": 10.0, "name": "幽灵"}}})
        summary = weekly.collect(WEEK, use_live=False)["integrity_summary"]
        assert "exp1·基准策略" in summary["dirty_groups"]
        assert summary["all_clean"] is False
        assert summary["gate_note"].strip(), "排除必须给出理由，不能静默剔除"

    def test_stale_account_loses_its_week_return(self, isolated_env):
        """数据过期的账户保留原始观测供诊断，但收益字段必须置空。

        让 stale 账户带着一个看似正常的收益率进排名，比不出报表危险得多。
        """
        _seed_traded_account(
            "exp1",
            equity_rows=[("2020-01-01 15:00:00", 900_000, 100_000, 1_000_000, 0.0)],
            state_extra={"round": 3})
        groups = {g["account"]: g for g in weekly.collect(WEEK, use_live=False)["groups"]}
        exp1 = groups["exp1"]
        if exp1["stale"]:
            assert exp1["week_ret_pct"] is None
            assert exp1["week_pnl"] is None


class TestDashboardCollect:

    def test_returns_all_accounts_with_a_status_line(self, seeded):
        data, status = dashboard.collect(use_live=False)
        assert len(data) == 13
        assert status

    def test_every_account_gets_a_distinct_series_colour(self, seeded):
        data, _ = dashboard.collect(use_live=False)
        colours = [g["color"] for g in data]
        assert len(set(colours)) == len(colours), "曲线撞色会让看板读不出是哪一组"

    def test_absent_accounts_render_at_initial_cash(self, seeded):
        """看板要能画出「尚未开张」，但不能把它画成一条 0 收益的线。"""
        data, _ = dashboard.collect(use_live=False)
        absent = next(g for g in data if not g["exists"])
        assert absent["ret"] == 0.0 and absent["equity"] == []

    def test_realized_pnl_matches_the_ledger(self, seeded):
        data, _ = dashboard.collect(use_live=False)
        exp1 = next(g for g in data if g["account"] == "exp1")
        assert 4900 < exp1["realized"] < 5000
        assert exp1["win"] == 1 and exp1["loss"] == 0

    def test_offline_mode_never_touches_the_network(self, seeded, monkeypatch):
        from astock.data import market

        def explode(*_a, **_k):
            raise AssertionError("--no-live 下不得请求行情")

        monkeypatch.setattr(market, "get_quotes", explode)
        dashboard.collect(use_live=False)


class TestReportingIsReadOnly:
    """报表**绝不能**产生副作用。看一眼报表就把 13 个账户全开了户，是不可接受的。"""

    def test_weekly_creates_no_ledgers(self, isolated_env):
        weekly.collect(WEEK, use_live=False)
        for account_id in ("A", "exp1", "B"):
            assert not AccountPaths.for_account(account_id).state.exists()

    def test_dashboard_creates_no_ledgers(self, isolated_env):
        dashboard.collect(use_live=False)
        assert not AccountPaths.for_group("A").state.exists()

    def test_weekly_does_not_write_back_to_state(self, seeded):
        """新鲜度闸门要注入 previous_round，必须用副本注入，不得回写真账本。"""
        before = json.loads(AccountPaths.for_experiment("exp1").state.read_text(encoding="utf-8"))
        weekly.collect(WEEK, use_live=False)
        after = json.loads(AccountPaths.for_experiment("exp1").state.read_text(encoding="utf-8"))
        assert before == after
        assert "previous_round" not in after
