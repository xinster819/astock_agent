"""
trade_guard 的 TDD 测试 —— 执行层幂等/防抖 + 跨进程互斥。
=================================================================
根因复盘：run_experiment 是"读state→生成信号→下单→写state"。两个进程在数秒内
同时启动会都读到满血 state，各下一次单，后写覆盖先写 → trades 记两笔、state 只剩一笔
（幽灵成交）。exp4/exp5 重复间隔 6-8s 即此并发窗口。

被测契约：
  trade_guard.can_execute(state, now=None, cooldown_sec=60) -> (ok: bool, reason: str)
     ok=True  时把 now 写入 state['last_run_ts']（epoch 秒）
     ok=False 当距 last_run_ts 不足 cooldown_sec（判为重复触发）
  trade_guard.account_lock(key, ttl_sec=600) 上下文管理器：
     同一 key 同时只允许一个持有者；重复获取抛 LockBusy；异常/正常退出都释放。
"""
import os
import time
import unittest

from astock.guards import trade as tg


class TestCooldownDedup(unittest.TestCase):
    def test_first_run_ok(self):
        st = {}
        ok, _ = tg.can_execute(st, now=1000.0, cooldown_sec=60)
        self.assertTrue(ok)
        self.assertEqual(st["last_run_ts"], 1000.0)

    def test_immediate_second_run_rejected(self):
        st = {}
        tg.can_execute(st, now=1000.0, cooldown_sec=60)
        ok, reason = tg.can_execute(st, now=1008.0, cooldown_sec=60)  # 8s 后，exp4 的真实间隔
        self.assertFalse(ok, "冷却期内的重复触发必须被拒")
        self.assertIn("冷却", reason)
        # 被拒时不得刷新时间戳（否则可无限延后）
        self.assertEqual(st["last_run_ts"], 1000.0)

    def test_run_after_cooldown_ok(self):
        st = {}
        tg.can_execute(st, now=1000.0, cooldown_sec=60)
        ok, _ = tg.can_execute(st, now=1061.0, cooldown_sec=60)
        self.assertTrue(ok)
        self.assertEqual(st["last_run_ts"], 1061.0)

    def test_boundary_exactly_cooldown_ok(self):
        st = {}
        tg.can_execute(st, now=1000.0, cooldown_sec=60)
        ok, _ = tg.can_execute(st, now=1060.0, cooldown_sec=60)  # 恰好等于冷却
        self.assertTrue(ok)

    def test_default_now_uses_walltime(self):
        st = {}
        ok, _ = tg.can_execute(st, cooldown_sec=60)  # now 缺省=当前
        self.assertTrue(ok)
        self.assertAlmostEqual(st["last_run_ts"], time.time(), delta=5)


class TestAccountLock(unittest.TestCase):
    def setUp(self):
        self.key = f"test_lock_{os.getpid()}"

    def tearDown(self):
        # 清理可能残留的锁文件
        p = tg._lock_path(self.key)
        if os.path.exists(p):
            os.remove(p)

    def test_mutual_exclusion(self):
        with tg.account_lock(self.key), self.assertRaises(tg.LockBusy), \
                tg.account_lock(self.key):
            pass

    def test_release_allows_reacquire(self):
        with tg.account_lock(self.key):
            pass
        # 退出后应能再次获取
        with tg.account_lock(self.key):
            pass

    def test_lock_released_on_exception(self):
        try:
            with tg.account_lock(self.key):
                raise ValueError("boom")
        except ValueError:
            pass
        # 异常后锁必须已释放
        with tg.account_lock(self.key):
            pass

    def test_stale_lock_is_reclaimed(self):
        # 制造一个过期锁文件（ttl 之外），应被自动回收
        p = tg._lock_path(self.key)
        with open(p, "w") as f:
            f.write("99999\t1.0")  # 假 pid + 很久以前的时间戳
        old = time.time() - 10000
        os.utime(p, (old, old))
        with tg.account_lock(self.key, ttl_sec=600):
            pass  # 不应抛 LockBusy


class TestConcurrentSimulation(unittest.TestCase):
    """模拟"两进程数秒内先后到达"：第二个必须被冷却拒绝。"""

    def test_two_rapid_arrivals(self):
        st = {"cash": 1_000_000.0}
        executed = 0
        # 第一波
        ok, _ = tg.can_execute(st, now=5000.0, cooldown_sec=60)
        if ok:
            executed += 1
        # 6 秒后第二波（exp5 真实间隔）
        ok, _ = tg.can_execute(st, now=5006.0, cooldown_sec=60)
        if ok:
            executed += 1
        self.assertEqual(executed, 1, "数秒内两次到达只应放行一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
