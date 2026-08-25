"""pipeline.prepare · agent 决策包。

三段式流水的第一段。这份 JSON 是 agent 唯一的输入——它看到什么，就据此决定
买卖什么。所以决策包的**准确**与**完整**同等重要：

  · 少一个约束字段，agent 就会提出注定被硬闸拒掉的方案
  · 多一条不可信的行情，agent 就会据此下单
  · C/D 组少了新闻契约，agent 就会「凭训练记忆编新闻」——这正是 news_feed 要根治的

`execute` 那一段已有 21 个用例守着硬闸。这里守的是输入侧。
"""
import json

import pytest

from astock.core.account import Account
from astock.pipeline import prepare
from astock.runtime.paths import AccountPaths
from astock.strategy import signals

CODE = "600519"


@pytest.fixture(autouse=True)
def no_network_news(monkeypatch):
    """本文件考核的是决策包的组装，不是新闻取数。

    C/D 组会真的去拉个股新闻——不桩掉的话每个用例都要等外网。
    需要验证取数失败行为的用例可以再 monkeypatch 一次覆盖它。
    """
    from astock.data import news_feed
    monkeypatch.setattr(news_feed, "get_news_for_codes",
                        lambda codes: {"status": "ok", "items": []})


@pytest.fixture
def offline(monkeypatch):
    """钉死行情与指标，只考核决策包的组装。"""
    from astock.data import market

    monkeypatch.setattr(market, "is_trading_now", lambda now=None: (True, "交易中"))
    monkeypatch.setattr(market, "get_quotes", lambda codes: {
        c: {"code": c, "name": f"股票{c}", "price": 10.0, "prev_close": 9.8,
            "limit_up": 11.0, "limit_down": 9.0, "open": 9.9, "high": 10.2,
            "low": 9.7, "cross": "median(3源)"}
        for c in codes})
    monkeypatch.setattr(market, "log_spread", lambda quotes: None)
    monkeypatch.setattr(signals, "load_pool", lambda: [CODE])
    monkeypatch.setattr(signals, "_indicators", lambda code: {
        "code": code, "close": 10.0, "prev_close": 9.8, "ma5": 9.9, "ma10": 9.8,
        "ma20": 9.5, "prev_ma20": 9.4, "momentum": 0.05, "cross_up_ma20": True,
        "below_ma10": False, "golden_cross": False, "rsi14": 55.0, "volume_ratio": 1.4,
    })


class TestDecisionPack:

    def test_writes_the_pack_and_returns_it(self, offline, isolated_env):
        pack = prepare.build("B")
        path = AccountPaths.for_group("B").decision_input
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))["group"] == "B"
        assert pack["group"] == "B"

    def test_pack_is_written_atomically(self, offline, isolated_env):
        """决策包是 agent 回合的唯一输入。半截 JSON 会让 agent 读不出来，
        表现为「agent 莫名其妙不下单」——又一种静默失效。"""
        prepare.build("B")
        path = AccountPaths.for_group("B").decision_input
        json.loads(path.read_text(encoding="utf-8"))          # 能解析即完整
        assert not list(path.parent.glob("*.tmp"))

    def test_carries_every_hard_constraint(self, offline, isolated_env):
        """约束必须写全：agent 看不到的限制，只会让它提出注定被拒的方案。"""
        constraints = prepare.build("B")["constraints"]
        assert constraints["max_positions"] == signals.MAX_POSITIONS
        assert constraints["max_weight_per_stock"] == signals.MAX_WEIGHT
        assert constraints["max_new_per_round"] == signals.MAX_NEW_PER_ROUND
        assert constraints["budget_per_stock"] > 0
        assert "硬校验" in constraints["note"]

    def test_reports_account_state(self, offline, isolated_env):
        account = prepare.build("B")["account"]
        assert account["cash"] == 1_000_000.0
        assert account["total_asset"] == 1_000_000.0
        assert account["return_pct"] == 0.0
        assert account["round"] == 0

    def test_includes_rule_candidates_as_advice(self, offline, isolated_env):
        """规则候选是参谋不是命令，但必须给——agent 需要锚点。"""
        assert isinstance(prepare.build("B")["rule_candidates"], list)

    def test_includes_indicators_and_quotes(self, offline, isolated_env):
        pack = prepare.build("B")
        assert CODE in pack["indicators"]
        assert pack["quotes"][CODE]["price"] == 10.0
        assert "limit_up" in pack["quotes"][CODE]


class TestPositionsSnapshot:

    def test_reports_holdings_with_unrealised_pnl(self, offline, isolated_env):
        account = Account.open("B")
        account.buy({"code": CODE, "name": "贵州茅台", "price": 8.0,
                     "limit_up": 9.0, "limit_down": 7.0}, 1000)
        account.save()

        position = prepare.build("B")["positions"][CODE]
        assert position["qty"] == 1000
        assert position["available"] == 0, "当日买入 T+1 冻结，agent 必须看得见"
        assert position["pnl_pct"] > 0, "现价 10 高于成本 8，应显示浮盈"

    def test_settles_before_snapshotting(self, offline, isolated_env):
        """跨日结算要发生在拍快照之前，否则 agent 会以为仓位还冻着。"""
        account = Account.open("B")
        account.buy({"code": CODE, "name": "贵州茅台", "price": 8.0,
                     "limit_up": 9.0, "limit_down": 7.0}, 1000)
        account.state["last_settle_date"] = "2020-01-01"
        account.save()

        assert prepare.build("B")["positions"][CODE]["available"] == 1000


class TestQuoteQuality:
    """行情质量必须显式告诉 agent，否则它会拿脏价当真。"""

    def test_flags_bad_quotes(self, offline, isolated_env, monkeypatch):
        from astock.data import market

        monkeypatch.setattr(market, "get_quotes", lambda codes: {
            c: {"code": c, "name": "脏价", "price": 0, "diverge": "三源分歧>0.5%"}
            for c in codes})
        quality = prepare.build("B")["quote_quality"]
        assert CODE in quality["bad"]

    def test_flags_single_source_degradation(self, offline, isolated_env, monkeypatch):
        from astock.data import market

        monkeypatch.setattr(market, "get_quotes", lambda codes: {
            c: {"code": c, "name": "降级", "price": 10.0,
                "cross": "single_source(eastmoney)"}
            for c in codes})
        quality = prepare.build("B")["quote_quality"]
        assert CODE in quality["single_source"]
        assert CODE not in quality["bad"], "单源是降级放行，不是拒单"


class TestNewsGate:
    """C/D 组注入真实新闻，并附使用铁律——压制「无源即编」的幻觉。"""

    def test_group_b_gets_no_news(self, offline, isolated_env):
        assert "news" not in prepare.build("B")

    @pytest.mark.parametrize("group", ["C", "D"])
    def test_news_groups_get_news_and_the_contract(self, offline, isolated_env, group):
        pack = prepare.build(group)
        assert "news" in pack
        gate = pack["news_gate"]
        assert "禁止凭记忆补充" in gate
        assert "不得假设" in gate

    def test_news_failure_degrades_without_blocking(self, offline, isolated_env,
                                                    monkeypatch):
        """新闻取数失败不该拖垮整个决策包——但失败本身必须写进包里。"""
        from astock.data import news_feed

        def boom(_codes):
            raise RuntimeError("新闻源不可用")

        monkeypatch.setattr(news_feed, "get_news_for_codes", boom)
        pack = prepare.build("C")
        assert "_error" in pack["news"]
        assert pack["rule_candidates"] is not None, "主流程仍应完整"


class TestGroupIsolation:

    def test_each_group_writes_to_its_own_file(self, offline, isolated_env):
        for group in ("B", "C", "D"):
            prepare.build(group)
        for group in ("B", "C", "D"):
            path = AccountPaths.for_group(group).decision_input
            assert json.loads(path.read_text(encoding="utf-8"))["group"] == group

    def test_group_defaults_to_env(self, offline, isolated_env, monkeypatch):
        monkeypatch.setenv("ASTOCK_GROUP", "D")
        assert prepare.build()["group"] == "D"

    def test_prepare_does_not_create_other_groups(self, offline, isolated_env):
        prepare.build("B")
        assert not AccountPaths.for_group("C").decision_input.exists()
