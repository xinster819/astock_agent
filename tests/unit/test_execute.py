"""pipeline.execute · agent 决策落地。

核心契约：**规则候选只是参谋，agent 是决策者**——但 agent 绕不过硬闸。
本文件把"绕不过"这件事逐条钉死：非法决策、单票权重、持仓数上限、
市场状态下的新开仓额度。任何一条松掉，agent 就能把账户打穿。
"""
import json

import pytest

from astock.core.account import Account
from astock.pipeline import execute, round_engine
from astock.pipeline.round_engine import RoundPolicy, run_round
from astock.runtime.paths import AccountPaths
from astock.strategy import signals

CODE = "600000"


# =========================================================== 决策格式校验

class TestValidateDecision:
    """宽进严出：错一条不作废整个文件，但错的那条必须被显式报出来。

    静默丢弃会让"agent 明明下了单却没成交"变成无从追查的怪事。
    """

    def test_accepts_a_well_formed_decision(self):
        ok, result = execute.validate_decision(
            {"action": "BUY", "code": "600519", "qty": "500", "reason": "突破"})
        assert ok
        assert result == {"action": "buy", "code": "600519", "qty": 500, "reason": "突破"}

    @pytest.mark.parametrize("payload,fragment", [
        ("不是对象", "非对象"),
        ({"action": "hold", "code": "600519", "qty": 1}, "非法动作"),
        ({"action": "buy", "code": "60051", "qty": 1}, "非法代码"),
        ({"action": "buy", "code": "60051X", "qty": 1}, "非法代码"),
        ({"action": "buy", "code": "600519", "qty": "多"}, "非法数量"),
        ({"action": "buy", "code": "600519", "qty": 0}, "数量<=0"),
        ({"action": "buy", "code": "600519", "qty": -100}, "数量<=0"),
    ])
    def test_rejects_malformed_decisions_with_a_reason(self, payload, fragment):
        ok, reason = execute.validate_decision(payload)
        assert not ok
        assert fragment in reason

    def test_reason_is_truncated(self):
        """agent 可能写很长，账本备注列不该被它撑爆。"""
        ok, result = execute.validate_decision(
            {"action": "buy", "code": "600519", "qty": 1, "reason": "长" * 500})
        assert ok
        assert len(result["reason"]) == execute.REASON_MAX_LEN


# =========================================================== 新开仓额度

class TestNewEntryBudget:

    def test_risk_off_blocks_all_new_entries(self):
        assert execute._new_entry_budget("risk_off") == 0

    def test_high_volatility_allows_at_most_one(self):
        assert execute._new_entry_budget("high_volatility") == 1

    def test_normal_uses_the_configured_cap(self):
        assert execute._new_entry_budget("normal") == signals.MAX_NEW_PER_ROUND


# =========================================================== 端到端硬闸

@pytest.fixture
def offline(monkeypatch):
    from astock.data import market

    monkeypatch.setattr(market, "is_trading_now", lambda now=None: (True, "交易中"))
    monkeypatch.setattr(market, "get_quotes", lambda codes: {
        c: {"code": c, "name": c, "price": 10.0, "limit_up": 11.0, "limit_down": 9.0}
        for c in codes})
    monkeypatch.setattr(market, "log_spread", lambda quotes: None)
    monkeypatch.setattr(market, "sample_spreads", lambda: (0, None))
    monkeypatch.setattr(signals, "load_pool", lambda: [CODE])
    monkeypatch.setattr(round_engine, "_current_regime", lambda config, out: "normal")


@pytest.fixture
def group_b(isolated_env):
    return AccountPaths.for_group("B").ensure_dirs()


def _write_decisions(paths, decisions, *, input_ts="2026-08-25 10:00:00"):
    """写一对新鲜的 decision_input / decision_output。"""
    paths.decision_input.write_text(
        json.dumps({"ts": input_ts}), encoding="utf-8")
    paths.decision_output.write_text(
        json.dumps({"input_ts": input_ts, "decisions": decisions}), encoding="utf-8")
    # output 的 mtime 必须晚于 input，否则会被新鲜度校验判为上轮残留
    import os
    stat = paths.decision_input.stat()
    os.utime(paths.decision_output, (stat.st_atime + 10, stat.st_mtime + 10))


def _run_agent_round(paths, *, force=True):
    return run_round(paths.account, execute.agent_decider(paths, force=force),
                     config={"name": "t"}, policy=RoundPolicy(use_risk_guard=False),
                     force=force, verbose=False)


class TestAgentCannotBypassPortfolioLimits:

    def test_single_stock_weight_is_capped(self, offline, group_b):
        """agent 要求买 10 万股，落账的绝不会超过 MAX_WEIGHT 允许的仓位。"""
        _write_decisions(group_b, [
            {"action": "buy", "code": CODE, "qty": 100_000, "reason": "梭哈"}])
        report = _run_agent_round(group_b)

        assert len(report.fills) == 1
        filled_value = report.fills[0].amount
        assert filled_value <= 1_000_000.0 * signals.MAX_WEIGHT + 1

    def test_position_count_cap_blocks_new_names(self, offline, group_b, monkeypatch):
        from astock.data import market

        codes = [f"60000{i}" for i in range(signals.MAX_POSITIONS + 2)]
        monkeypatch.setattr(market, "get_quotes", lambda _codes: {
            c: {"code": c, "name": c, "price": 10.0, "limit_up": 11.0} for c in codes})
        monkeypatch.setattr(signals, "load_pool", lambda: codes)
        _write_decisions(group_b, [
            {"action": "buy", "code": c, "qty": 100, "reason": "分散"} for c in codes])

        report = _run_agent_round(group_b)
        assert len(report.fills) <= signals.MAX_POSITIONS

    def test_illegal_decision_is_reported_not_silently_dropped(self, offline, group_b):
        _write_decisions(group_b, [{"action": "teleport", "code": CODE, "qty": 1}])
        report = _run_agent_round(group_b)
        assert report.fills == []
        assert any("非法决策" in line for line in report.lines)

    def test_force_tag_is_written_into_the_ledger(self, offline, group_b):
        """强制轮次的成交备注要带标记，让账本自己说明这笔不是正常时段产生的。"""
        _write_decisions(group_b, [{"action": "buy", "code": CODE, "qty": 100, "reason": "r"}])
        _run_agent_round(group_b, force=True)
        note = Account.open("B").ledger.read_trades()[0]["备注"]
        assert "[强制/非交易时段]" in note
        assert "agentB:" in note


class TestStaleDecisionsAreRefused:

    def test_stale_output_produces_no_orders(self, offline, group_b):
        """事故现场：决策文件早于本轮决策包 —— 拿旧决策按新价格下单。"""
        import os
        _write_decisions(group_b, [{"action": "buy", "code": CODE, "qty": 100, "reason": "r"}])
        stat = group_b.decision_input.stat()
        os.utime(group_b.decision_output, (stat.st_atime - 100, stat.st_mtime - 100))

        report = _run_agent_round(group_b)
        assert report.fills == []
        assert any("新鲜度" in line for line in report.lines)

    def test_missing_output_still_refreshes_equity(self, offline, group_b):
        """没有决策文件就不下单——但权益曲线要保持连续。"""
        report = _run_agent_round(group_b)
        assert report.fills == []
        assert report.ordered is True
        assert len(Account.open("B").ledger.read_equity()) == 1

    def test_corrupt_json_is_refused_loudly(self, offline, group_b):
        group_b.decision_input.write_text('{"ts": "x"}', encoding="utf-8")
        group_b.decision_output.write_text("{ 半截 JSON", encoding="utf-8")
        report = _run_agent_round(group_b)
        assert report.fills == []
        assert any("解析失败" in line for line in report.lines)


class TestArchiving:

    def test_consumed_decision_is_archived_not_deleted(self, offline, group_b):
        """归档而非删除：决策与成交要能一一对上，事后才能重放核对。"""
        _write_decisions(group_b, [{"action": "buy", "code": CODE, "qty": 100, "reason": "r"}])
        execute.execute("B", force=True, verbose=False)

        assert not group_b.decision_output.exists()
        archived = list(group_b.root.glob("decision_output_*.json"))
        assert len(archived) == 1
        assert json.loads(archived[0].read_text(encoding="utf-8"))["decisions"]

    def test_decision_log_records_fills(self, offline, group_b):
        _write_decisions(group_b, [{"action": "buy", "code": CODE, "qty": 100, "reason": "r"}])
        execute.execute("B", force=True, verbose=False)
        rows = [line for line in group_b.decision_log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        assert len(rows) == 2, "表头 + 一行成交"
