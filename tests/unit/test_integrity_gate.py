"""
integrity_gate 的 TDD 测试。
========================================
先写测试再写实现。覆盖 6 类不变量 + 1 个真实脏数据端到端 + 1 个干净数据阴性对照。

被测契约（integrity_gate.check）：
    result = integrity_gate.check(trades, state, init_cash=1_000_000.0, pool=None)
    -> {
         "clean": bool,               # 无任何 error 级红旗则 True
         "red_flags": [               # 每条: {code(检查项), severity, detail, ...}
             {"check": "cash_direction", "severity": "error", "detail": ...},
             ...
         ],
       }
trades: list[dict]，键与 trades.csv 表头一致（时间/方向/代码/名称/价格/数量/成交额/费用/现金余额/备注）
state:  dict，同 state.json（cash / positions / init_cash ...）

严重级：error = 数据可信度被破坏（净值不可信）；warn = 需人工看一眼但不必然污染。
"""
import unittest
import os
import csv

from astock.guards import integrity as ig

BASE = os.path.dirname(os.path.abspath(__file__))


def _flags_of(result, check):
    return [f for f in result["red_flags"] if f["check"] == check]


def _has(result, check, severity=None):
    fs = _flags_of(result, check)
    if severity:
        fs = [f for f in fs if f["severity"] == severity]
    return len(fs) > 0


# ---------- 合成夹具 ----------

def clean_trades():
    """一买一卖，现金方向正确，账实一致。"""
    return [
        {"时间": "2026-06-30 11:05:45", "方向": "买入", "代码": "300782", "名称": "卓胜微",
         "价格": "107.62", "数量": "1800", "成交额": "193716.0", "费用": "50.37",
         "现金余额": "806233.63", "备注": "cross_up_ma20 动量6.5%"},
        {"时间": "2026-07-01 14:07:40", "方向": "卖出", "代码": "300782", "名称": "卓胜微",
         "价格": "105.45", "数量": "1800", "成交额": "189810.0", "费用": "239.16",
         "现金余额": "995804.47", "备注": "跌破MA10 盈亏-4195.56"},
    ]


def clean_state():
    # 全部卖出后空仓，现金 = 重放结果
    # 1_000_000 - (193716.0+50.37) + (189810.0-239.16) = 995804.47
    return {"cash": 995804.47, "init_cash": 1000000.0, "positions": {}}


def dup_order_trades():
    """exp4 式重复下单：同一票 8 秒内买两次，第二次现金反而涨回（幽灵成交）。"""
    return [
        {"时间": "2026-06-29 14:16:23", "方向": "买入", "代码": "002371", "名称": "北方华创",
         "价格": "831.97", "数量": "200", "成交额": "166394.0", "费用": "43.26",
         "现金余额": "833562.74", "备注": "ma5_cross_ma20 动量41.1%"},
        {"时间": "2026-06-29 14:16:31", "方向": "买入", "代码": "002371", "名称": "北方华创",
         "价格": "829.8", "数量": "200", "成交额": "165960.0", "费用": "43.15",
         "现金余额": "833996.85", "备注": "ma5_cross_ma20 动量41.2%"},
    ]


def dup_order_state():
    # 幽灵成交被覆盖，state 里只有一笔 200 股
    return {"cash": 833996.85, "init_cash": 1000000.0,
            "positions": {"002371": {"qty": 200, "available": 200, "cost": 830.0, "name": "北方华创"}}}


class TestCashDirection(unittest.TestCase):
    """买入必减现金、卖出必增现金；违反=幽灵成交铁证。"""

    def test_clean_no_flag(self):
        r = ig.check(clean_trades(), clean_state(), init_cash=1_000_000.0)
        self.assertFalse(_has(r, "cash_direction"))

    def test_buy_that_increases_cash_is_flagged(self):
        r = ig.check(dup_order_trades(), dup_order_state(), init_cash=1_000_000.0)
        self.assertTrue(_has(r, "cash_direction", "error"),
                        "买入后现金余额上涨必须被判为 error")


class TestDuplicateOrders(unittest.TestCase):
    """同(代码,方向)在极短时间窗内重复出现。"""

    def test_dup_detected(self):
        r = ig.check(dup_order_trades(), dup_order_state(), init_cash=1_000_000.0)
        self.assertTrue(_has(r, "duplicate_order"),
                        "8 秒内同票同向两笔应判为重复下单")

    def test_clean_no_dup(self):
        r = ig.check(clean_trades(), clean_state(), init_cash=1_000_000.0)
        self.assertFalse(_has(r, "duplicate_order"))


class TestStateReconciliation(unittest.TestCase):
    """重放 trades 得到的净持仓/现金必须等于 state。"""

    def test_qty_mismatch_flagged(self):
        # trades 净买入 400 股，state 只有 200 → 账实不符
        r = ig.check(dup_order_trades(), dup_order_state(), init_cash=1_000_000.0)
        self.assertTrue(_has(r, "state_mismatch", "error"))

    def test_clean_reconciles(self):
        r = ig.check(clean_trades(), clean_state(), init_cash=1_000_000.0)
        self.assertFalse(_has(r, "state_mismatch"))


class TestNegativeCash(unittest.TestCase):
    def test_negative_cash_flagged(self):
        st = {"cash": -123.0, "init_cash": 1_000_000.0, "positions": {}}
        r = ig.check([], st, init_cash=1_000_000.0)
        self.assertTrue(_has(r, "negative_cash", "error"))

    def test_positive_cash_ok(self):
        r = ig.check(clean_trades(), clean_state(), init_cash=1_000_000.0)
        self.assertFalse(_has(r, "negative_cash"))


class TestCleanIsClean(unittest.TestCase):
    """干净账户必须整体判定 clean=True，零 error 红旗。"""

    def test_clean_true(self):
        r = ig.check(clean_trades(), clean_state(), init_cash=1_000_000.0)
        errors = [f for f in r["red_flags"] if f["severity"] == "error"]
        self.assertEqual(errors, [], f"干净账户不应有 error 红旗，实得: {errors}")
        self.assertTrue(r["clean"])


class TestRealExp4EndToEnd(unittest.TestCase):
    """用真实被污染的 exp4 账本做端到端：必须判脏并同时命中 3 类。
    注：exp4_trades.csv 已被 clean_ghost_trades.py 清洗，脏账本保留在
    最新的 .bak 备份里；本用例读备份以继续验证闸门对真实脏数据的检出能力。
    """

    def _dirty_trades_path(self):
        import glob
        exp_dir = os.path.join(BASE, "experiments")
        baks = sorted(glob.glob(os.path.join(exp_dir, "exp4_trades.csv.bak.*")))
        if baks:
            return baks[-1]                        # 最新备份=清洗前的脏账本
        return os.path.join(exp_dir, "exp4_trades.csv")  # 尚未清洗时回退到原文件

    def _load(self):
        tp = self._dirty_trades_path()
        sp = os.path.join(BASE, "experiments", "exp4_state.json")
        with open(tp, encoding="utf-8") as f:
            trades = list(csv.DictReader(f))
        import json
        with open(sp, encoding="utf-8") as f:
            state = json.load(f)
        return trades, state

    def test_exp4_flagged_dirty(self):
        if not os.path.exists(os.path.join(BASE, "experiments", "exp4_state.json")):
            self.skipTest("exp4 数据缺失")
        trades, state = self._load()
        r = ig.check(trades, state, init_cash=state.get("init_cash", 1_000_000.0))
        self.assertFalse(r["clean"], "被污染的 exp4 必须判为 not clean")
        self.assertTrue(_has(r, "cash_direction", "error"))
        self.assertTrue(_has(r, "duplicate_order"))
        self.assertTrue(_has(r, "state_mismatch", "error"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
