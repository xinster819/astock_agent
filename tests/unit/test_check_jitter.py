"""ops.check_jitter · 抖动与超时截断核对。

调度器对单条命令有 10 分钟硬上限、超时直接 SIGKILL。抖动会占掉其中一部分，
所以「进程在睡眠中被杀」是一种真实且高频的失效——而它的表现是
**那一轮什么都没发生**，与「非交易时段正常跳过」完全一样。

jitter_log 的 sleeping/fired 双行就是为了让这两者可分辨；本模块负责读出结论。
和停摆自检一样，一个自己会静默失效的截断检测器比没有更糟——所以它需要测试。
"""
import datetime as dt

import pytest

from astock.core.ledger import EQUITY_COLUMNS
from astock.ops import check_jitter as cj
from astock.runtime import files, paths

NOW = dt.datetime(2026, 8, 25, 10, 30)


def jrow(wake, planned, fired="", actual="", status="fired"):
    return {"唤醒时刻": wake, "计划延时s": str(planned), "实际开跑时刻": fired,
            "实际延时s": str(actual), "状态": status}


class TestPairing:

    def test_pairs_sleeping_with_fired(self):
        rows = [jrow("10:00:05", 120, status="sleeping"),
                jrow("10:00:05", 120, "10:02:05", 120)]
        paired = cj.pair_jitter_rows(rows, 10)
        assert set(paired["10:00:05"]) == {"sleeping", "fired"}

    def test_filters_to_the_target_hour(self):
        rows = [jrow("09:00:05", 60, status="sleeping"),
                jrow("10:00:05", 60, status="sleeping")]
        assert list(cj.pair_jitter_rows(rows, 10)) == ["10:00:05"]

    def test_unparseable_wake_times_are_skipped(self):
        assert cj.pair_jitter_rows([jrow("坏数据", 60)], 10) == {}


class TestTruncationDetection:
    """核心判据：只有 sleeping、没有 fired = 进程在睡眠中被杀。"""

    def test_orphan_sleep_is_flagged_as_truncation(self):
        paired = cj.pair_jitter_rows([jrow("10:00:05", 300, status="sleeping")], 10)
        verdict = cj.judge_jitter(paired)
        assert verdict.truncated is True
        assert "超时截断" in verdict.message
        assert verdict.level == "高", "截断是硬结论，不能压成低优先级"

    def test_complete_pair_is_not_truncation(self):
        paired = cj.pair_jitter_rows(
            [jrow("10:00:05", 120, status="sleeping"),
             jrow("10:00:05", 120, "10:02:05", 120)], 10)
        verdict = cj.judge_jitter(paired)
        assert verdict.truncated is False
        assert "抖动真实生效" in verdict.message

    def test_large_delay_drift_is_reported_but_not_truncation(self):
        """睡了 120s 却过了 400s 才开跑：调度器挂起过，但进程活着。"""
        paired = cj.pair_jitter_rows([jrow("10:00:05", 120, "10:06:45", 400)], 10)
        verdict = cj.judge_jitter(paired)
        assert verdict.truncated is False
        assert "偏差大" in verdict.message

    def test_orphan_sleeps_are_listed_separately(self):
        rows = [jrow("10:00:05", 300, status="sleeping"),
                jrow("10:20:05", 120, status="sleeping"),
                jrow("10:20:05", 120, "10:22:05", 120)]
        paired = cj.pair_jitter_rows(rows, 10)
        assert cj.orphan_sleeps(paired) == ["10:00:05"]

    def test_no_records_is_not_reported_as_truncation(self):
        """没记录可能只是用了 --no-jitter，不能误报成故障。"""
        verdict = cj.judge_jitter({})
        assert verdict.truncated is False


class TestEquityProduction:

    @pytest.fixture
    def equity(self, isolated_env):
        path = paths.AccountPaths.for_experiment("exp1").equity
        return path

    def test_finds_the_row_produced_in_the_hour(self, equity):
        files.append_csv_row(equity, EQUITY_COLUMNS,
                             ["2026-08-25 10:07:30", 1000, 2000, 3000, 5.0])
        found = cj.latest_equity_in_hour(equity, 10, dt.date(2026, 8, 25))
        assert found and found[0].minute == 7

    def test_returns_none_when_the_hour_is_empty(self, equity):
        files.append_csv_row(equity, EQUITY_COLUMNS,
                             ["2026-08-25 09:07:30", 1000, 2000, 3000, 5.0])
        assert cj.latest_equity_in_hour(equity, 10, dt.date(2026, 8, 25)) is None

    def test_compares_the_full_date_not_just_the_day_number(self, equity):
        """回归：旧实现只比日号（几号），会把**上个月同一天**的旧行

        当成本轮产物，恰恰在停摆故障上发出虚假绿灯。
        """
        files.append_csv_row(equity, EQUITY_COLUMNS,
                             ["2026-07-25 10:07:30", 1000, 2000, 3000, 5.0])
        assert cj.latest_equity_in_hour(equity, 10, dt.date(2026, 8, 25)) is None

    def test_picks_the_latest_row_within_the_hour(self, equity):
        for minute in (2, 9, 5):
            files.append_csv_row(equity, EQUITY_COLUMNS,
                                 [f"2026-08-25 10:0{minute}:00", 1000, 2000, 3000, 5.0])
        found = cj.latest_equity_in_hour(equity, 10, dt.date(2026, 8, 25))
        assert found[0].minute == 9

    def test_missing_file_is_not_an_error(self, isolated_env):
        path = paths.AccountPaths.for_experiment("exp9").equity
        assert cj.latest_equity_in_hour(path, 10, dt.date(2026, 8, 25)) is None


class TestDelayMinutes:

    def test_measures_from_the_top_of_the_hour(self):
        assert cj.delay_minutes(dt.datetime(2026, 8, 25, 10, 7, 30)) == 7.5

    def test_exactly_on_the_hour_is_zero(self):
        assert cj.delay_minutes(dt.datetime(2026, 8, 25, 10, 0, 0)) == 0.0


class TestFullCheck:

    def test_covers_every_non_control_account(self, isolated_env):
        """回归：旧实现在这里硬编码了第 6 份账户表，且写的是旧名称。"""
        lines = []
        _, groups = cj.check(10, now=NOW, printer=lines.append)
        assert len(groups) == 12, "A 组单独深度核对，其余 12 组走产出健康度"
        assert "exp9·多因子横截面排序" in "\n".join(lines), "名称应随配置更新"

    def test_reports_missing_production(self, isolated_env):
        lines = []
        cj.check(10, now=NOW, printer=lines.append)
        assert "0/12 组已产出" in "\n".join(lines)

    def test_reports_healthy_production(self, isolated_env):
        for account in paths.all_accounts():
            files.append_csv_row(account.equity, EQUITY_COLUMNS,
                                 ["2026-08-25 10:08:00", 1000, 2000, 3000, 5.0])
        lines = []
        cj.check(10, now=NOW, printer=lines.append)
        assert "12/12 组已产出" in "\n".join(lines)

    def test_cli_command_runs(self, isolated_env, capsys):
        from astock.cli.main import main

        assert main(["check-jitter", "10"]) == 0
        assert "抖动" in capsys.readouterr().out
