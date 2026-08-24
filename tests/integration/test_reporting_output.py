"""报表产出：落盘的 JSON 与渲染出的 HTML。

采集算得对不等于报表出得来。这里验证最后一段——写文件与渲染——
不会在真实数据上炸掉，也不会悄悄产出一份残缺的东西。
"""
import json
import pathlib

import pytest

from astock.cli.main import main as cli_main
from astock.core.account import Account
from astock.reporting import dashboard, report, weekly
from astock.runtime import paths

WEEK = "2026-W34"


@pytest.fixture
def one_account(isolated_env):
    """一个有持仓、有成交的账户，其余 12 个不存在。"""
    account = Account.open("exp1")
    quote = {"code": "600519", "name": "贵州茅台", "price": 10.0,
             "limit_up": 11.0, "limit_down": 9.0}
    account.buy(quote, 1000, reason="cross_up_ma20 动量3%")
    account.snapshot_equity({"600519": quote})
    account.save()
    return account


class TestWeeklyOutput:

    def test_writes_the_data_base_and_returns_its_path(self, one_account):
        out = weekly.main(week_str=WEEK, use_live=False, printer=lambda *_: None)
        assert out.exists()
        assert out.name == f"weekly_data_{WEEK}.json"

    def test_output_lands_in_the_reports_directory(self, one_account, isolated_env):
        """报表产物与账本分开存放：账本丢了不可恢复，报表丢了重跑就有。"""
        out = weekly.main(week_str=WEEK, use_live=False, printer=lambda *_: None)
        assert out.parent == paths.reports_dir()
        assert out.parent != isolated_env

    def test_output_is_valid_json_with_the_expected_shape(self, one_account):
        out = weekly.main(week_str=WEEK, use_live=False, printer=lambda *_: None)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert set(data) >= {"meta", "integrity_summary", "indices",
                             "groups", "research_universe"}
        assert len(data["groups"]) == 13

    def test_console_summary_names_every_account(self, one_account):
        lines = []
        weekly.main(week_str=WEEK, use_live=False, printer=lambda *a: lines.append(" ".join(map(str, a))))
        text = "\n".join(lines)
        assert "exp1·基准策略" in text
        assert "数据完整性闸门" in text

    def test_previous_week_rounds_are_read_back(self, one_account):
        """上周的数据底座是 non_advancing_round 闸门的唯一输入源。

        这条闸门本来就写好了，但一直是死代码——它要求 state 里有 previous_round，
        而全代码库无一处写入。现在靠上周落盘的 JSON 补上。
        """
        weekly.main(week_str="2026-W33", use_live=False, printer=lambda *_: None)
        rounds = weekly._prev_week_rounds("2026-W33")
        assert "exp1" in rounds, "应能按 account id 取回上周轮次"

    def test_missing_previous_week_is_silent(self, isolated_env):
        """首次复盘没有上周文件，应保持沉默而不是误报。"""
        assert weekly._prev_week_rounds("1999-W01") == {}


class TestDashboardOutput:

    def test_renders_a_complete_html_document(self, one_account):
        data, status = dashboard.collect(use_live=False)
        out = dashboard.render(data, live_status=status, use_live=False)
        html = pathlib.Path(out).read_text(encoding="utf-8")
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")

    def test_html_lands_in_the_reports_directory(self, one_account):
        data, status = dashboard.collect(use_live=False)
        out = dashboard.render(data, live_status=status, use_live=False)
        assert str(paths.reports_dir()) in out

    def test_every_account_appears_in_the_page(self, one_account):
        data, status = dashboard.collect(use_live=False)
        html = pathlib.Path(
            dashboard.render(data, live_status=status, use_live=False)
        ).read_text(encoding="utf-8")
        for group in data:
            assert group["name"] in html, f"{group['name']} 没出现在看板里"

    def test_payload_is_embedded_as_parseable_json(self, one_account):
        """看板把数据内嵌进页面。序列化失败会渲染出一个空图表而不报错。"""
        data, status = dashboard.collect(use_live=False)
        html = pathlib.Path(
            dashboard.render(data, live_status=status, use_live=False)
        ).read_text(encoding="utf-8")
        assert json.dumps(data, ensure_ascii=False)[:40] in html or '"exp1' in html


class TestAccountReport:
    """`astock report` —— 13 个账户的横向对比是对照实验的主视图。"""

    def test_summary_table_lists_every_account(self, one_account):
        text = report.summary_table(use_live=False)
        for account_id in ("A", "exp1", "exp9", "B", "D"):
            assert account_id in text

    def test_summary_is_column_aligned(self, one_account):
        """对比表要能一眼扫完，列错位就废了。"""
        lines = [line for line in report.summary_table(use_live=False).split("\n")
                 if line.startswith(("A ", "exp", "B ", "C ", "D "))]
        assert len({len(line) for line in lines}) <= 2, "各行宽度应基本一致"

    def test_single_account_report_shows_positions_and_trades(self, one_account):
        text = report.account_report("exp1", use_live=False)
        assert "贵州茅台" in text
        assert "当前持仓" in text
        assert "成交明细" in text

    def test_report_on_an_untouched_account_is_not_an_error(self, one_account):
        """没开张的账户也要能出报告，而不是抛异常。"""
        text = report.account_report("exp9", use_live=False)
        assert "当前空仓" in text
        assert "暂无成交记录" in text

    def test_experiment_report_uses_the_configured_name(self, one_account):
        assert "基准策略" in report.account_report("exp1", use_live=False)


class TestReportingCommands:
    """CLI 层：三个只读命令都不该产生副作用或非零退出。"""

    @pytest.mark.parametrize("argv", [
        ["report", "--offline"],
        ["report", "exp1", "--offline"],
        ["weekly", "--week", WEEK, "--offline"],
        ["dashboard", "--offline"],
    ])
    def test_command_succeeds(self, one_account, argv, capsys):
        assert cli_main(argv) == 0
        assert capsys.readouterr().out.strip()
