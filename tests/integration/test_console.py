"""reporting.console · 观察台的装配与产出。

观察台是给人看的，而人会照着它做决定。所以这里测的重点是
**它会不会把噪音渲染成结论**，以及产出的 HTML 是不是真的自包含、离线可用。
"""
import json
import re

import pytest

from astock.core.account import Account
from astock.reporting import console
from astock.runtime import paths


def _quote(code="600519", price=10.0, name="贵州茅台"):
    return {"code": code, "name": name, "price": price,
            "limit_up": price * 1.1, "limit_down": price * 0.9}


@pytest.fixture
def seeded(isolated_env):
    """exp1 有持仓与一笔已平仓；exp2 只有持仓；其余 11 个未初始化。"""
    a = Account.open("exp1")
    a.buy(_quote(price=10.0), 1000, reason="cross_up_ma20")
    a.state["positions"]["600519"]["available"] = 1000
    a.sell(_quote(price=12.0), 1000, reason="止盈")
    a.buy(_quote("000001", price=20.0, name="平安银行"), 500, reason="cross_up_ma20")
    a.snapshot_equity({})
    a.save()

    b = Account.open("exp2")
    b.buy(_quote(price=10.0), 1000, reason="cross_up_ma20")
    b.snapshot_equity({})
    b.save()
    return isolated_env


@pytest.fixture
def payload(seeded):
    return console.build(use_live=False)


class TestPayloadShape:

    def test_carries_every_section_the_ui_needs(self, payload):
        assert set(payload) >= {"meta", "verdict", "thresholds", "health",
                                "accounts", "overlap", "benchmark"}

    def test_covers_all_thirteen_accounts(self, payload):
        assert len(payload["accounts"]) == 13

    def test_each_account_carries_its_layer(self, payload):
        layers = {a["account"]: a["layer"] for a in payload["accounts"]}
        assert layers["A"] == "control"
        assert layers["exp1"] == "rule"
        assert layers["D"] == "agent"

    def test_strategy_name_is_bare_not_the_full_label(self, payload):
        """界面上账户 id 已经单独一列，`name` 那种 "exp1·基准策略" 会拼成
        "exp1 · exp1·基准策略"。所以另给一个裸策略名。"""
        exp1 = next(a for a in payload["accounts"] if a["account"] == "exp1")
        assert exp1["strategy"] == "基准策略"
        assert exp1["name"] == "exp1·基准策略"


class TestVerdictRefusesToOverclaim:
    """观察台的核心职责：样本不够时明确说不能比。"""

    def test_thin_samples_are_not_comparable(self, payload):
        assert payload["verdict"]["ok"] is False
        assert payload["verdict"]["eligible"] == []

    def test_headline_carries_no_icon(self, payload):
        """图标由展示层给。数据里带 ⚠ 会导致渲染出两个符号。"""
        assert "⚠" not in payload["verdict"]["headline"]

    def test_reasons_are_specific_not_generic(self, payload):
        assert payload["verdict"]["reasons"]
        assert any("平仓" in r for r in payload["verdict"]["reasons"])

    def test_thresholds_are_published_to_the_ui(self, payload):
        """界面要能说清「门槛是多少」，不能只给一个结论。"""
        t = payload["thresholds"]
        assert t["min_trades_for_signal"] < t["min_trades_for_comparison"]


class TestHealth:

    def test_flags_accounts_that_never_closed_a_trade(self, payload):
        """从未平仓的账户，其收益全是浮盈，不构成业绩——必须点名。"""
        assert "exp2" in payload["health"]["never_closed"]
        assert "exp1" not in payload["health"]["never_closed"]

    def test_reports_ledger_integrity(self, payload):
        assert payload["health"]["dirty"] == []
        assert payload["health"]["all_clear"] is True


class TestAccountEnrichment:

    def test_computes_trade_stats(self, payload):
        exp1 = next(a for a in payload["accounts"] if a["account"] == "exp1")
        assert exp1["trade_stats"]["closed"] == 1
        assert exp1["trade_stats"]["wins"] == 1

    def test_small_sample_is_not_judged(self, payload):
        exp1 = next(a for a in payload["accounts"] if a["account"] == "exp1")
        assert exp1["trade_stats"]["edge_is_detectable"] is None
        assert exp1["tier"]["rank_eligible"] is False

    def test_uninitialised_accounts_are_marked_not_zeroed(self, payload):
        """未开张 ≠ 0 收益。看板不能把缺失渲染成持平。"""
        d = next(a for a in payload["accounts"] if a["account"] == "D")
        assert d["exists"] is False
        assert d["trade_stats"] is None

    def test_overlap_finds_shared_holdings(self, payload):
        shared = {h["code"]: h for h in payload["overlap"]}
        assert "600519" in shared
        assert set(shared["600519"]["held_by"]) == {"exp2"} or \
               "exp2" in shared["600519"]["held_by"]

    def test_offline_mode_reports_no_benchmark(self, payload):
        """取不到基准就说没有，绝不画一条平的假线。"""
        assert payload["benchmark"] is None


class TestRenderedPage:

    def test_writes_a_self_contained_document(self, payload):
        out = console.render(payload)
        html = out.read_text(encoding="utf-8")
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")

    def test_has_no_external_dependencies(self, payload):
        """必须能在断网的机器上双击打开。任何外链都会让它变成半张白纸。"""
        html = console.render(payload).read_text(encoding="utf-8")
        assert "<script src=" not in html
        assert "<link" not in html
        assert not re.search(r'(src|href)\s*=\s*["\']https?://', html)

    def test_all_placeholders_are_filled(self, payload):
        html = console.render(payload).read_text(encoding="utf-8")
        for token in ("__CSS__", "__JS__", "__PAYLOAD__"):
            assert token not in html

    def test_payload_is_valid_json_in_the_page(self, payload):
        html = console.render(payload).read_text(encoding="utf-8")
        raw = html.split('<script id="payload" type="application/json">')[1].split("</script>")[0]
        assert json.loads(raw)["accounts"]

    def test_escapes_angle_brackets_in_the_payload(self, seeded):
        """成交备注是 agent 自由文本。一个 `</script>` 就会提前闭合脚本块，
        页面从此错乱——而错乱的看板不会报错，只会显示错的东西。"""
        account = Account.open("exp1")
        account.buy(_quote("000002", price=10.0), 100, reason="</script><b>注入</b>")
        account.save()

        html = console.render(console.build(use_live=False)).read_text(encoding="utf-8")
        raw = (html.split('<script id="payload" type="application/json">')[1]
                   .split("</script>")[0])
        assert "<" not in raw, "负载里出现了未转义的 `<`，脚本块会被提前闭合"
        assert "注入" in raw, "转义不能把数据本身弄丢"
        assert json.loads(raw.replace("\\u003c", "<"))["accounts"]

    def test_lands_in_the_reports_directory(self, payload):
        assert console.render(payload).parent == paths.reports_dir()


class TestBenchmarkAlignment:
    """基准必须对齐到账户的观察期，否则这个对比没有意义。

    负载里带的是 180 天指数历史，而账户只跑了约两个月。拿「账户自 7 月 +1%」
    比「指数自 3 月 +5%」是错的——实测对齐后同期沪深300 是 −8.58%，
    而多数账户在 −1%~0%，这层背景光看账户收益率完全读不出来。
    """

    def _benchmark(self, closes):
        return {"name": "沪深300", "code": "000300", "points": [
            {"t": f"2026-{m:02d}-{d:02d}", "close": c}
            for (m, d), c in closes.items()]}

    def _accounts(self, first, last):
        return [{"account": "exp1", "exists": True,
                 "equity": [{"t": f"{first} 10:00:00", "total": 1.0},
                            {"t": f"{last} 15:00:00", "total": 1.0}]}]

    def test_return_is_measured_inside_the_account_window(self):
        bench = self._benchmark({(3, 1): 100.0, (7, 1): 200.0, (8, 1): 220.0})
        accounts = self._accounts("2026-07-01", "2026-08-01")
        console._align_benchmark(bench, accounts)
        # 只看 7/1 → 8/1，不该把 3 月起的翻倍算进来
        assert bench["window_return_pct"] == 10.0
        assert bench["window"] == ["2026-07-01", "2026-08-01"]
        assert bench["window_points"] == 2

    def test_points_outside_the_window_are_excluded(self):
        bench = self._benchmark({(3, 1): 100.0, (7, 1): 100.0, (8, 1): 90.0, (12, 1): 500.0})
        console._align_benchmark(bench, self._accounts("2026-07-01", "2026-08-01"))
        assert bench["window_points"] == 2
        assert bench["window_return_pct"] == -10.0

    def test_too_few_points_leaves_it_unannotated(self):
        """窗口里不足两个点就不给结论，而不是编一个。"""
        bench = self._benchmark({(3, 1): 100.0})
        console._align_benchmark(bench, self._accounts("2026-07-01", "2026-08-01"))
        assert "window_return_pct" not in bench

    def test_missing_benchmark_is_a_noop(self):
        console._align_benchmark(None, self._accounts("2026-07-01", "2026-08-01"))

    def test_accounts_without_equity_leave_it_unannotated(self):
        bench = self._benchmark({(7, 1): 100.0, (8, 1): 110.0})
        console._align_benchmark(bench, [{"account": "A", "exists": False}])
        assert "window_return_pct" not in bench


class TestProgress:
    """带实时行情时这条命令要跑好几分钟（14 只持仓逐只做三源交叉验证，
    实测单只约 10 秒）。全程没有输出的话，人会以为它挂了——实测确实会。"""

    def test_reports_each_stage(self, seeded):
        stages = []
        console.build(use_live=False, progress=stages.append)
        assert len(stages) >= 4
        assert any("账本" in s for s in stages)
        assert any("完成" in s for s in stages)

    def test_progress_is_optional(self, seeded):
        console.build(use_live=False)   # 不给回调也要能跑

    def test_offline_mode_says_it_is_skipping_the_network(self, seeded):
        stages = []
        console.build(use_live=False, progress=stages.append)
        assert any("离线" in s for s in stages)


class TestCliCommand:

    def test_dashboard_command_succeeds_and_reports_the_verdict(self, seeded, capsys):
        from astock.cli.main import main

        assert main(["dashboard", "--offline"]) == 0
        out = capsys.readouterr().out
        assert "观察台已生成" in out
        assert "还不能下结论" in out, "命令行也要把判据说出来，不能只给个文件路径"
        assert "[1/5]" in out, "长耗时命令必须给进度，否则看起来像挂了"
