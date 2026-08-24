"""
执行层幂等锁 · 集成测试（run_exp / execute 两个下单入口)
=================================================================
验证护栏真正拦住"数秒内重复触发"：第二次调用必须跳过下单循环、不产生第二笔交易。
用真账户文件跑会污染数据，故这里对 broker/market/strategy 打桩，只考核护栏逻辑接线。
"""
import unittest
import os
import sys
import time
import types
import datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from astock.guards import trade as tg


class TestRunExpGuardWiring(unittest.TestCase):
    """run_exp.run_experiment：锁内加载 state + 冷却去抖，重复触发不再下单。"""

    def setUp(self):
        from astock.pipeline import run_exp
        self.run_exp = run_exp
        self.key = f"exp_itest_{os.getpid()}"
        # 内存态账户，避免碰真文件
        self.mem_state = {"cash": 1_000_000.0, "init_cash": 1_000_000.0,
                          "positions": {}, "round": 0, "exp_id": self.key}
        self.buy_calls = []

        from astock.core import experiments as exp_manager
        self._orig = {}
        # 打桩 exp_manager：配置存在、状态走内存
        self._patch(exp_manager, "get_exp_config", lambda e: {"name": "itest"})
        self._patch(exp_manager, "load_exp_state", lambda e: dict(self.mem_state))
        def _save(e, st):
            self.mem_state = dict(st)   # 落盘=更新内存态（含 last_run_ts）
        self._patch(exp_manager, "save_exp_state", _save)

        from astock.data import market
        from astock.strategy import signals as strategy
        self._patch(market, "is_trading_now", lambda now: (True, "交易中"))
        self._patch(market, "get_quotes", lambda codes: {c: {"code": c, "name": c,
                    "price": 10.0, "limit_up": 11.0, "limit_down": 9.0} for c in codes})
        self._patch(market, "log_spread", lambda q: None)
        self._patch(market, "sample_spreads", lambda: (0, None))
        self._patch(strategy, "load_pool", lambda: ["600000"])
        # 每轮都想买一笔，用于观测护栏是否拦截
        self._patch(strategy, "generate_signals",
                    lambda st, quotes, exp_config=None: [
                        {"action": "buy", "code": "600000", "qty": 100, "reason": "itest"}])
        # 记录真实下单调用
        self._patch(self.run_exp, "_buy_exp",
                    lambda st, q, qty, reason, exp_id: (self.buy_calls.append(1), (True, "buy"))[1])
        self._patch(self.run_exp, "_log_equity_exp", lambda *a, **k: None)

    def _patch(self, mod, name, fn):
        self._orig[(mod, name)] = getattr(mod, name)
        setattr(mod, name, fn)

    def tearDown(self):
        for (mod, name), fn in self._orig.items():
            setattr(mod, name, fn)
        p = tg._lock_path(self.key)
        if os.path.exists(p):
            os.remove(p)

    def test_second_rapid_call_skips_ordering(self):
        # 第一次：应放行并下单
        self.run_exp.run_experiment(self.key, force=True, verbose=False)
        self.assertEqual(len(self.buy_calls), 1, "首轮必须下单")
        self.assertIn("last_run_ts", self.mem_state, "首轮须写入 last_run_ts")
        # 数秒后第二次：冷却期内，必须跳过下单循环
        # 手动把 last_run_ts 拨到 8 秒前，模拟真实并发窗口
        self.mem_state["last_run_ts"] = dt.datetime.now().timestamp() - 8
        self.run_exp.run_experiment(self.key, force=True, verbose=False)
        self.assertEqual(len(self.buy_calls), 1, "冷却期内第二次触发不得再下单")

    def test_after_cooldown_orders_again(self):
        self.run_exp.run_experiment(self.key, force=True, verbose=False)
        self.assertEqual(len(self.buy_calls), 1)
        # 冷却期外
        self.mem_state["last_run_ts"] = dt.datetime.now().timestamp() - 120
        self.run_exp.run_experiment(self.key, force=True, verbose=False)
        self.assertEqual(len(self.buy_calls), 2, "过冷却期应重新下单")


class TestLockSerializesExecution(unittest.TestCase):
    """账户锁被占用时，run_experiment 直接跳过（不抛异常、不下单)。"""

    def test_busy_lock_skips(self):
        from astock.pipeline import run_exp
        from astock.core import experiments as exp_manager
        key = f"exp_busy_{os.getpid()}"
        orig_cfg = exp_manager.get_exp_config
        exp_manager.get_exp_config = lambda e: {"name": "busy"}
        try:
            with tg.account_lock(key):
                # 锁已被本测试持有，内部再抢应 LockBusy → 被吞掉、安全返回
                res = run_exp.run_experiment(key, force=True, verbose=False)
            self.assertIn("跳过本轮", res)
        finally:
            exp_manager.get_exp_config = orig_cfg
            p = tg._lock_path(key)
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
