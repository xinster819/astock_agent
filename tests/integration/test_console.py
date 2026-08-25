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


class TestCliCommand:

    def test_dashboard_command_succeeds_and_reports_the_verdict(self, seeded, capsys):
        from astock.cli.main import main

        assert main(["dashboard", "--offline"]) == 0
        out = capsys.readouterr().out
        assert "观察台已生成" in out
        assert "还不能下结论" in out, "命令行也要把判据说出来，不能只给个文件路径"
