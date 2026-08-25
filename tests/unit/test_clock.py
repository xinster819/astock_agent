"""交易所时钟 + 交易时段判定的回归测试。

这些用例直接对应 2026-07-31 ~ 08-21 的停摆事故：
进程时区不是北京时，is_trading_now() 用裸本地时间比 9:30-15:00，
把北京 14:00 读成 06:00 → "未开盘" → 12 个账户连续三周零成交。

核心断言：**无论进程时区是什么，北京时间的交易时段判定必须一致。**
"""
import datetime as dt
import os
import time
import unittest
from typing import ClassVar

from astock.data import market
from astock.runtime import clock as market_time

BEIJING = market_time.MARKET_TZ
# 2026-08-25 是周二
TRADING_DAY = dt.date(2026, 8, 25)
SATURDAY = dt.date(2026, 8, 22)


def _beijing(day, hour, minute=0):
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=BEIJING)


class _TZCase(unittest.TestCase):
    """在指定进程时区下运行，退出时恢复原时区。"""

    PROCESS_TZ = None

    def setUp(self):
        self._saved = os.environ.get("TZ")
        if self.PROCESS_TZ:
            os.environ["TZ"] = self.PROCESS_TZ
            time.tzset()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._saved
        time.tzset()

    def as_process_naive(self, aware):
        """调用方 dt.datetime.now() 在当前进程时区下会拿到的朴素值。"""
        return aware.astimezone().replace(tzinfo=None)


class TestTradingWindowIsTimezoneIndependent(_TZCase):
    """同一北京时刻，在任何进程时区下判定都必须相同。"""

    SESSION: ClassVar = [(9, 30), (10, 0), (11, 30), (13, 0), (14, 0), (15, 0)]
    CLOSED: ClassVar = [(8, 0), (9, 29), (11, 31), (12, 0), (15, 1), (18, 0)]

    def _assert_all(self, tzname):
        self.PROCESS_TZ = tzname
        self.setUp()
        try:
            for h, m in self.SESSION:
                aware = _beijing(TRADING_DAY, h, m)
                naive = self.as_process_naive(aware)
                self.assertTrue(market.is_trading_now(naive)[0],
                                f"[{tzname}] 北京 {h:02d}:{m:02d} 应判交易中，"
                                f"进程读到 {naive}")
                self.assertTrue(market.is_trading_now(aware)[0])
            for h, m in self.CLOSED:
                aware = _beijing(TRADING_DAY, h, m)
                naive = self.as_process_naive(aware)
                self.assertFalse(market.is_trading_now(naive)[0],
                                 f"[{tzname}] 北京 {h:02d}:{m:02d} 不应判交易中")
        finally:
            self.tearDown()

    def test_process_tz_shanghai(self):
        self._assert_all("Asia/Shanghai")

    def test_process_tz_utc_the_actual_outage(self):
        """原服务器上 exp*/B/C/D 进程的真实处境 —— 事故复现用例。"""
        self._assert_all("UTC")

    def test_process_tz_us_pacific_the_migration_target(self):
        """本次迁移目标机器的时区。UTC 下账本日期尚属巧合正确，这里连日期都会错位。"""
        self._assert_all("America/Los_Angeles")

    def test_process_tz_india_half_hour_offset(self):
        """半小时偏移时区，验证归一化不是简单的整点加减。"""
        self._assert_all("Asia/Kolkata")


class TestWeekend(_TZCase):
    PROCESS_TZ = "UTC"

    def test_saturday_is_closed_even_at_session_time(self):
        aware = _beijing(SATURDAY, 14, 0)
        ok, status = market.is_trading_now(self.as_process_naive(aware))
        self.assertFalse(ok)
        self.assertEqual(status, "周末休市")


class TestEnforceFixesLedgerDateLabels(_TZCase):
    """仅修 is_trading_now 管不到账本日期标签，enforce() 才管得到。"""

    PROCESS_TZ = "America/Los_Angeles"

    def test_offset_is_not_utc8_before_enforce(self):
        self.assertFalse(market_time.offset_ok())

    def test_enforce_makes_process_local_time_beijing(self):
        self.assertTrue(market_time.enforce())
        self.assertTrue(market_time.offset_ok())
        self.assertEqual(dt.datetime.now().astimezone().utcoffset(),
                         dt.timedelta(hours=8))

    def test_enforce_makes_naive_now_match_market_clock(self):
        market_time.enforce()
        # broker._today() / risk_date / trades.csv 时间列走的都是这个朴素本地时钟
        self.assertEqual(dt.datetime.now().strftime("%Y-%m-%d"),
                         market_time.today())

    def test_enforce_is_idempotent(self):
        self.assertTrue(market_time.enforce())
        self.assertTrue(market_time.enforce())
        self.assertTrue(market_time.offset_ok())


class TestToMarket(_TZCase):
    PROCESS_TZ = "UTC"

    def test_naive_is_interpreted_as_process_local(self):
        # 进程 TZ=UTC 时，朴素 06:00 就是北京 14:00
        got = market_time.to_market(dt.datetime(2026, 8, 25, 6, 0))
        self.assertEqual(got.strftime("%Y-%m-%d %H:%M"), "2026-08-25 14:00")

    def test_aware_is_converted_not_reinterpreted(self):
        aware = dt.datetime(2026, 8, 25, 6, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(market_time.to_market(aware).strftime("%H:%M"), "14:00")

    def test_none_returns_current_market_time(self):
        self.assertEqual(market_time.to_market().utcoffset(), dt.timedelta(hours=8))

    def test_market_tz_has_no_dst(self):
        """A股无夏令时：全年偏移恒为 +8，固定偏移退化实现与 IANA 等价。"""
        for month in range(1, 13):
            probe = dt.datetime(2026, month, 15, 12, tzinfo=market_time.MARKET_TZ)
            self.assertEqual(probe.utcoffset(), dt.timedelta(hours=8))


if __name__ == "__main__":
    unittest.main()
