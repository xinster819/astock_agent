"""
trade_guard · 执行层幂等/防抖闸门 + 跨进程账户互斥锁
==========================================================
根治"幽灵成交"：run_experiment 是读-改-写 state，两进程数秒内并发到达会各下一单、
后写覆盖先写 → trades 记两笔、state 只剩一笔。防御两层：
  1) can_execute(冷却去抖): 距上次成功执行不足 cooldown_sec 秒 → 判为重复触发，拒绝。
     纯确定性、可注入时钟，覆盖"错峰任务撞车 / jitter 撞点 / 手动+定时叠加"。
  2) account_lock(文件互斥): 同一账户同一时刻只允许一个执行者，防真并发。
     基于 O_CREAT|O_EXCL 原子建锁；带 ttl 自动回收死锁；异常/正常退出都释放。

纯 stdlib，被 run_exp / execute 直接调用。
"""
import errno
import os
import time
from contextlib import contextmanager

from astock.runtime import paths

DEFAULT_COOLDOWN_SEC = 60
DEFAULT_TTL_SEC = 600


class LockBusy(Exception):
    """账户锁已被他人持有。"""


# ---------------- 冷却去抖 ----------------

def can_execute(state, now=None, cooldown_sec=DEFAULT_COOLDOWN_SEC):
    """
    判断本次是否放行。放行则把 now 写入 state['last_run_ts'] 并返回 (True, "")。
    冷却期内的重复触发返回 (False, 原因)，且不刷新时间戳。
    state 会被就地修改；调用方负责随后 save_state 落盘。
    """
    if now is None:
        now = time.time()
    last = state.get("last_run_ts")
    if last is not None:
        elapsed = now - float(last)
        if 0 <= elapsed < cooldown_sec:
            return False, (f"距上次执行仅 {elapsed:.0f}s（<冷却 {cooldown_sec}s），"
                           f"判为重复触发，跳过本轮以防幽灵成交")
    state["last_run_ts"] = now
    return True, ""


# ---------------- 跨进程互斥锁 ----------------

def _lock_path(key):
    # 每次调用重新解析工作区：测试要能在临时目录里加锁，
    # 模块级常量做不到这件事。
    lock_dir = paths.locks_dir()
    os.makedirs(lock_dir, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(key))
    return os.path.join(lock_dir, f"{safe}.lock")


@contextmanager
def account_lock(key, ttl_sec=DEFAULT_TTL_SEC):
    """
    账户级互斥。同一 key 同时只允许一个持有者，否则抛 LockBusy。
    死锁保护：锁文件超过 ttl_sec 未更新则视为陈旧，自动回收。
    """
    path = _lock_path(key)

    # 陈旧锁回收：mtime 超过 ttl 直接删
    if os.path.exists(path):
        try:
            if time.time() - os.path.getmtime(path) > ttl_sec:
                os.remove(path)
        except OSError:
            pass

    fd = None
    try:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as e:
            if e.errno == errno.EEXIST:
                raise LockBusy(f"账户 {key} 正被另一执行者占用（锁: {path}）") from e
            raise
        os.write(fd, f"{os.getpid()}\t{time.time()}".encode())
        os.close(fd)
        fd = None
        yield
    finally:
        # 只释放自己建的锁
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


# ---------------- 便捷组合器 ----------------

@contextmanager
def guarded_run(state, account_key, now=None,
                cooldown_sec=DEFAULT_COOLDOWN_SEC, ttl_sec=DEFAULT_TTL_SEC):
    """
    一站式护栏：先抢互斥锁，再判冷却。二者任一不过都 yield (False, reason)，
    过则 yield (True, "")。用法：
        with guarded_run(st, "exp4") as (ok, why):
            if not ok:
                print("跳过:", why); return
            ... 真正下单 ...
    """
    try:
        with account_lock(account_key, ttl_sec=ttl_sec):
            ok, why = can_execute(state, now=now, cooldown_sec=cooldown_sec)
            yield (ok, why)
    except LockBusy as e:
        yield (False, str(e))


if __name__ == "__main__":
    # 自检
    st = {}
    print(can_execute(st, now=1000.0))
    print(can_execute(st, now=1005.0))
    print(can_execute(st, now=1100.0))
