"""reporting.metrics · 报表算术。

周报里的每个数字都是对照实验的**结论**：某组本周涨了多少、赢了几笔、
是否跑赢基准。这些数字算错比报表干脆出不来危险得多——错的数字看起来和对的
一模一样，而整个项目就是靠它们判断「规则决策 vs Agent 决策」孰优。

重构前这些算术埋在 weekly.collect() 那个 160 行函数中间，一行测试都没有。

贯穿本文件的一条判据：**取不到数据返回 None，绝不返回 0**。
0 是一个合法的收益率，「没有数据」不是——把二者混同就是在伪造净值。
"""
import datetime as dt
from typing import ClassVar

import pytest

from astock.reporting import metrics

MON = dt.datetime(2026, 8, 17)          # 2026-W34 周一
WED = dt.datetime(2026, 8, 19, 14, 0)


def eq(ts, total, ret=0.0):
    return {"时间": ts, "总资产": str(total), "累计收益率%": str(ret)}


def trade(ts, side="卖出", note="", code="600519"):
    return {"时间": ts, "方向": side, "代码": code, "名称": "测试",
            "价格": "10", "数量": "100", "成交额": "1000", "备注": note}


# =========================================================== 解析

class TestParseTime:

    @pytest.mark.parametrize("raw,expected", [
        ("2026-08-17 09:35:00", dt.datetime(2026, 8, 17, 9, 35)),
        ("2026-08-17", dt.datetime(2026, 8, 17)),
        (" 2026-08-17 09:35:00 ", dt.datetime(2026, 8, 17, 9, 35)),
    ])
    def test_accepts_both_ledger_formats(self, raw, expected):
        assert metrics.parse_time(raw) == expected

    @pytest.mark.parametrize("raw", ["", "昨天", "2026/08/17", None, 42, "2026-13-45"])
    def test_never_guesses(self, raw):
        """认不出就是 None。悄悄猜一个日期会让整周成交被划进错误的窗口。"""
        assert metrics.parse_time(raw) is None


class TestExtractRealizedPnl:

    @pytest.mark.parametrize("note,expected", [
        ("止损 -5.2% 盈亏-8733.24", -8733.24),
        ("止盈 盈亏 123.4", 123.4),
        ("跌破MA10 盈亏5362.4", 5362.4),
        ("平推 盈亏0", 0.0),
    ])
    def test_reads_the_pnl_out_of_free_text(self, note, expected):
        assert metrics.extract_realized_pnl(note) == expected

    def test_buy_note_has_no_pnl(self):
        """买入不产生已实现盈亏。"""
        assert metrics.extract_realized_pnl("cross_up_ma20 动量3.0%") is None

    def test_none_and_zero_are_different(self):
        """None = 这笔不是平仓；0.0 = 平了但不赚不亏。胜负统计要能分辨。"""
        assert metrics.extract_realized_pnl("买入") is None
        assert metrics.extract_realized_pnl("盈亏0") == 0.0

    def test_non_string_input(self):
        assert metrics.extract_realized_pnl(None) is None


class TestToFloat:

    def test_parses_numeric_strings(self):
        assert metrics.to_float("1234.56") == 1234.56

    def test_falls_back_on_garbage(self):
        assert metrics.to_float("") == 0.0
        assert metrics.to_float("—") == 0.0
        assert metrics.to_float(None, default=None) is None


# =========================================================== 周窗口

class TestWeekBounds:

    def test_derives_the_iso_week_from_now(self):
        w = metrics.week_bounds(now=WED)
        assert w.iso == "2026-W34"
        assert w.start == MON

    def test_explicit_week_string(self):
        assert metrics.week_bounds("2026-W34").start == MON

    def test_end_is_clamped_to_now_midweek(self):
        """周中跑周报时窗口不该延伸到未来。

        否则「本周至今」会被当成「整周」，与上一周的完整值直接对比不公平。
        """
        w = metrics.week_bounds(now=WED)
        assert w.end == WED

    def test_end_is_sunday_when_week_has_passed(self):
        later = dt.datetime(2026, 9, 1)
        w = metrics.week_bounds("2026-W34", now=later)
        assert w.end == dt.datetime(2026, 8, 23, 23, 59, 59)

    def test_previous_week_is_contiguous(self):
        """上周结束与本周开始之间不能有缝——缝里的成交会被两边都漏掉。"""
        w = metrics.week_bounds(now=WED)
        assert w.prev_end + dt.timedelta(seconds=1) == w.start
        assert w.prev_start == w.start - dt.timedelta(days=7)

    def test_works_across_a_year_boundary(self):
        """ISO 周的跨年是经典陷阱：2027-W01 的周一落在 2026 年 12 月。"""
        w = metrics.week_bounds("2027-W01")
        assert w.iso == "2027-W01"
        assert w.start.weekday() == 0


# =========================================================== 权益取点

class TestEquityPoints:

    curve: ClassVar = [eq("2026-08-14 15:00:00", 1_000_000, 0.0),
             eq("2026-08-17 10:00:00", 1_010_000, 1.0),
             eq("2026-08-19 15:00:00", 1_020_000, 2.0)]

    def test_last_point_before(self):
        p = metrics.last_point_before(self.curve, dt.datetime(2026, 8, 18))
        assert p.total == 1_010_000

    def test_first_point_after(self):
        p = metrics.first_point_after(self.curve, MON)
        assert p.total == 1_010_000

    def test_returns_none_when_nothing_qualifies(self):
        assert metrics.last_point_before(self.curve, dt.datetime(2020, 1, 1)) is None
        assert metrics.first_point_after(self.curve, dt.datetime(2030, 1, 1)) is None

    def test_unparseable_rows_are_skipped_not_fatal(self):
        """账本里混进一行坏数据，不该让整周报表崩掉。"""
        dirty = [*self.curve, {"时间": "坏数据", "总资产": "x"}]
        assert metrics.last_point_before(dirty, dt.datetime(2026, 8, 20)).total == 1_020_000

    def test_empty_curve(self):
        assert metrics.last_point_before([], WED) is None


# =========================================================== 周收益

class TestWeekReturn:

    window = metrics.week_bounds("2026-W34", now=dt.datetime(2026, 8, 23, 23, 59))

    def test_uses_previous_week_close_as_the_baseline(self):
        """起点取上周末最后一个点，不取本周首点。

        账户在周一开盘前就已持仓，用本周首个观测当起点会把周末跳空算丢。
        """
        curve: ClassVar = [eq("2026-08-14 15:00:00", 1_000_000),   # 上周五收盘
                 eq("2026-08-17 09:35:00", 1_050_000),   # 本周一开盘后（已跳空）
                 eq("2026-08-21 15:00:00", 1_100_000)]
        result = metrics.week_return(curve, self.window)
        assert result.pnl == 100_000.0      # 相对上周五，不是相对周一
        assert result.pct == 10.0

    def test_falls_back_to_first_point_for_a_new_account(self):
        curve: ClassVar = [eq("2026-08-17 09:35:00", 1_000_000),
                 eq("2026-08-21 15:00:00", 1_020_000)]
        assert metrics.week_return(curve, self.window).pnl == 20_000.0

    def test_returns_none_not_zero_when_data_is_missing(self):
        """核心判据：没有数据 ≠ 本周持平。"""
        result = metrics.week_return([], self.window)
        assert result.pnl is None and result.pct is None

    def test_returns_none_when_baseline_is_zero(self):
        """起点为 0 时百分比无意义，不能除出个 inf 塞进报表。"""
        curve: ClassVar = [eq("2026-08-14 15:00:00", 0), eq("2026-08-21 15:00:00", 100)]
        assert metrics.week_return(curve, self.window).pct is None

    def test_a_losing_week_is_negative(self):
        curve: ClassVar = [eq("2026-08-14 15:00:00", 1_000_000),
                 eq("2026-08-21 15:00:00", 900_000)]
        result = metrics.week_return(curve, self.window)
        assert result.pnl == -100_000.0
        assert result.pct == -10.0

    def test_points_after_the_window_are_excluded(self):
        """下周的观测不能算进本周——周报每周跑，窗口必须闭合。"""
        curve: ClassVar = [eq("2026-08-14 15:00:00", 1_000_000),
                 eq("2026-08-21 15:00:00", 1_100_000),
                 eq("2026-08-25 15:00:00", 2_000_000)]   # 下周
        assert metrics.week_return(curve, self.window).pnl == 100_000.0


# =========================================================== 成交统计

class TestTradesInWindow:

    window = metrics.week_bounds("2026-W34", now=dt.datetime(2026, 8, 23, 23, 59))

    def test_filters_to_the_window(self):
        rows = [trade("2026-08-14 10:00:00"),      # 上周
                trade("2026-08-18 10:00:00"),      # 本周
                trade("2026-08-25 10:00:00")]      # 下周
        assert len(metrics.trades_in_window(rows, self.window).trades) == 1

    def test_counts_wins_and_losses(self):
        rows = [trade("2026-08-18 10:00:00", note="止盈 盈亏500"),
                trade("2026-08-18 11:00:00", note="止损 盈亏-200"),
                trade("2026-08-19 10:00:00", note="止盈 盈亏300")]
        stats = metrics.trades_in_window(rows, self.window)
        assert (stats.wins, stats.losses) == (2, 1)
        assert stats.realized == 600.0

    def test_buys_count_as_trades_but_not_as_wins_or_losses(self):
        rows = [trade("2026-08-18 10:00:00", side="买入", note="cross_up_ma20 动量3%"),
                trade("2026-08-19 10:00:00", note="止盈 盈亏100")]
        stats = metrics.trades_in_window(rows, self.window)
        assert len(stats.trades) == 2
        assert stats.closed == 1

    def test_flat_close_is_neither_win_nor_loss(self):
        rows = [trade("2026-08-18 10:00:00", note="盈亏0")]
        stats = metrics.trades_in_window(rows, self.window)
        assert (stats.wins, stats.losses, stats.closed) == (0, 0, 0)
        assert stats.realized == 0.0

    def test_win_rate_is_none_without_closed_trades(self):
        """没有平仓交易时胜率是 None，不是 0%——0% 意味着「全亏」。"""
        rows = [trade("2026-08-18 10:00:00", side="买入", note="开仓")]
        assert metrics.trades_in_window(rows, self.window).win_rate is None

    def test_win_rate_computation(self):
        rows = [trade("2026-08-18 10:00:00", note="盈亏100"),
                trade("2026-08-18 11:00:00", note="盈亏100"),
                trade("2026-08-18 12:00:00", note="盈亏100"),
                trade("2026-08-18 13:00:00", note="盈亏-100")]
        assert metrics.trades_in_window(rows, self.window).win_rate == 75.0

    def test_unparseable_time_rows_are_dropped(self):
        rows = [trade("坏数据", note="盈亏999"), trade("2026-08-18 10:00:00", note="盈亏1")]
        stats = metrics.trades_in_window(rows, self.window)
        assert stats.realized == 1.0, "时间无法解析的行不该被算进任何一周"

    def test_preserves_the_original_columns(self):
        rows = [trade("2026-08-18 10:00:00", note="止盈 盈亏500", code="600519")]
        row = metrics.trades_in_window(rows, self.window).trades[0]
        assert row["code"] == "600519" and row["pnl"] == 500.0 and row["side"] == "卖出"
