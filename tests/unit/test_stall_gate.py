"""停摆闸门（freshness_gate.stalled_engine）的回归测试。

背景：2026-07-31 ~ 08-21，13 个账户里 12 个的进程照常运行、equity.csv 每天照写、
integrity_gate 全绿，唯独从未进入下单分支 —— round 冻结、零成交。
既有闸门全部放行：
  · integrity_gate  不交易的账本天然自洽；
  · stale_equity    非交易分支照写权益行，天天"新鲜"；
  · non_advancing_round  依赖 previous_round，而全代码库无一处写入，是死代码。

这组用例锁定新增的 stalled_engine 判据，以及"字段缺失时必须沉默"的边界。
"""
import datetime as dt
import unittest

from astock.guards import freshness as fg

REVIEW_END = dt.datetime(2026, 8, 21, 15, 0)
REVIEW_START = dt.datetime(2026, 8, 17)
FRESH_ROW = [{"时间": "2026-08-21 14:07:17", "总资产": "1000000"}]


def _check(state, rows=None):
    return fg.check(state or {}, rows if rows is not None else FRESH_ROW,
                    now=REVIEW_END, review_start=REVIEW_START, review_end=REVIEW_END)


def _checks(result):
    return {f["check"] for f in result["red_flags"]}


class TestStalledEngine(unittest.TestCase):

    def test_the_actual_outage_is_caught(self):
        """事故现场：权益新鲜，但最后一次下单轮次停在 07-30。"""
        result = _check({"round": 46, "last_trading_round_date": "2026-07-30"})
        self.assertIn("stalled_engine", _checks(result))
        self.assertFalse(result["fresh"])

    def test_legacy_state_falls_back_to_risk_date(self):
        """历史账本没有新字段，但 risk_date 同样只在下单分支写，可作回退判据。"""
        result = _check({"round": 46, "risk_date": "2026-07-30"})
        self.assertIn("stalled_engine", _checks(result))

    def test_new_field_wins_over_legacy_field(self):
        result = _check({"last_trading_round_date": "2026-08-21",
                         "risk_date": "2026-07-30"})
        self.assertNotIn("stalled_engine", _checks(result))

    def test_healthy_account_passes(self):
        result = _check({"round": 98, "last_trading_round_date": "2026-08-21"})
        self.assertEqual(result, {"fresh": True, "red_flags": []})

    def test_one_idle_trading_day_is_tolerated(self):
        """容忍单日调度失败/超时截断，不因一次抖动就报红。"""
        result = _check({"last_trading_round_date": "2026-08-20"})
        self.assertNotIn("stalled_engine", _checks(result))

    def test_weekend_gap_is_not_a_stall(self):
        """周五跑完、周一复盘：中间隔的是周末，不是交易日。"""
        result = fg.check(
            {"last_trading_round_date": "2026-08-21"},
            [{"时间": "2026-08-24 10:00:00"}],
            now=dt.datetime(2026, 8, 24, 11), review_start=dt.datetime(2026, 8, 24),
            review_end=dt.datetime(2026, 8, 24, 11),
        )
        self.assertNotIn("stalled_engine", _checks(result))

    def test_three_idle_trading_days_trips_the_gate(self):
        result = _check({"last_trading_round_date": "2026-08-18"})
        self.assertIn("stalled_engine", _checks(result))

    def test_detail_names_the_date_and_says_beta_only(self):
        result = _check({"last_trading_round_date": "2026-07-30"})
        detail = next(f["detail"] for f in result["red_flags"]
                      if f["check"] == "stalled_engine")
        self.assertIn("2026-07-30", detail)
        self.assertIn("beta", detail)

    def test_severity_is_error_so_the_account_is_excluded_from_ranking(self):
        result = _check({"last_trading_round_date": "2026-07-30"})
        flag = next(f for f in result["red_flags"] if f["check"] == "stalled_engine")
        self.assertEqual(flag["severity"], "error")


class TestSilentWhenUnjudgeable(unittest.TestCase):
    """字段都缺时必须沉默 —— 空 state 判 fresh 是既有契约，不能被这条新闸门破坏。"""

    def test_empty_state_stays_fresh(self):
        self.assertEqual(_check({}), {"fresh": True, "red_flags": []})

    def test_state_without_any_round_date_stays_fresh(self):
        self.assertEqual(_check({"round": 12, "cash": 1000.0}),
                         {"fresh": True, "red_flags": []})

    def test_unparseable_date_is_ignored_not_flagged(self):
        self.assertNotIn("stalled_engine",
                         _checks(_check({"last_trading_round_date": "不是日期"})))

    def test_null_date_is_ignored(self):
        self.assertNotIn("stalled_engine",
                         _checks(_check({"last_trading_round_date": None,
                                         "risk_date": None})))


class TestPreviousRoundIsNoLongerDeadCode(unittest.TestCase):
    """weekly_collect 现在会从上周的数据底座注入 previous_round，这条闸门终于能生效。"""

    def test_frozen_round_is_flagged(self):
        result = _check({"round": 46, "previous_round": 46,
                         "last_trading_round_date": "2026-08-21"})
        self.assertIn("non_advancing_round", _checks(result))

    def test_advancing_round_passes(self):
        result = _check({"round": 98, "previous_round": 83,
                         "last_trading_round_date": "2026-08-21"})
        self.assertEqual(result, {"fresh": True, "red_flags": []})

    def test_regressed_round_is_flagged(self):
        result = _check({"round": 40, "previous_round": 46,
                         "last_trading_round_date": "2026-08-21"})
        self.assertIn("non_advancing_round", _checks(result))


if __name__ == "__main__":
    unittest.main()
