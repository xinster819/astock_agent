"""
source_health · 数据源健康度熔断（只省延迟，不改判定结果）
=====================================================================
背景：2026-08-23 实测东方财富 push2/push2his 整站 502。它在两个热路径上：
  · quote_sources.fetch_all —— 三源交叉验证的一员
  · market.get_hist        —— 个股日线的首选源
每次调用都要先失败一遍（含重试）才轮到可用源，实测每只票白白多花约 2 秒。
全池 50 只 × 13 个账户，一轮下来光是等一个已知挂掉的源就要几十分钟。

机制：连续失败 fail_threshold 次 → 熔断 cooldown_sec 秒，期间直接跳过该源；
冷却期满自动放行一次探测，成功即完全恢复。

【边界】这只影响"要不要浪费时间去问一个已知挂掉的源"，不影响任何判定口径：
  · 熔断的源本来就在返回 error，对多源交叉验证的贡献为零；
  · 交叉验证仍要求 >=2 个【有效】源且极差 <= DIVERGE_TOL，阈值一字未改；
  · 有效源不足时照旧 price=0 拒单。
换句话说：熔断前后成交与否完全一致，只是更快知道结果。

纯 stdlib，可注入时钟便于测试。
"""
import time as _time
import threading

DEFAULT_FAIL_THRESHOLD = 3      # 连续失败几次才熔断（容忍偶发抖动）
DEFAULT_COOLDOWN_SEC = 300      # 熔断多久后放行一次探测（5 分钟）


class SourceHealth:
    """线程安全的按源熔断器。"""

    def __init__(self, fail_threshold=DEFAULT_FAIL_THRESHOLD,
                 cooldown_sec=DEFAULT_COOLDOWN_SEC, clock=None):
        if fail_threshold < 1:
            raise ValueError("fail_threshold must be >= 1")
        if cooldown_sec < 0:
            raise ValueError("cooldown_sec cannot be negative")
        self.fail_threshold = int(fail_threshold)
        self.cooldown_sec = float(cooldown_sec)
        self._clock = clock or _time.time
        self._lock = threading.Lock()
        self._fails = {}        # name -> 连续失败次数
        self._open_until = {}   # name -> 熔断到期时刻

    def should_skip(self, name):
        """该源当前是否处于熔断期（True = 本次直接跳过，别浪费时间）。"""
        with self._lock:
            until = self._open_until.get(name)
            if until is None:
                return False
            if self._clock() >= until:
                # 冷却期满：放行一次探测，但不清零失败计数——
                # 探测失败会立刻重新熔断，探测成功才真正恢复。
                del self._open_until[name]
                return False
            return True

    def record_ok(self, name):
        with self._lock:
            self._fails.pop(name, None)
            self._open_until.pop(name, None)

    def record_fail(self, name):
        with self._lock:
            n = self._fails.get(name, 0) + 1
            self._fails[name] = n
            if n >= self.fail_threshold:
                self._open_until[name] = self._clock() + self.cooldown_sec

    def state(self):
        """给观测用的快照：{源名: {'fails':n, 'open_for':剩余秒}}。"""
        with self._lock:
            now = self._clock()
            return {
                name: {
                    "fails": self._fails.get(name, 0),
                    "open_for": round(max(0.0, self._open_until.get(name, now) - now), 1),
                }
                for name in set(self._fails) | set(self._open_until)
            }

    def reset(self):
        with self._lock:
            self._fails.clear()
            self._open_until.clear()


# 进程级单例。行情源的健康状况是进程内共享的事实。
QUOTES = SourceHealth()
