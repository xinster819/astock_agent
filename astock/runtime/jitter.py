"""jitter · 调度抖动：把整点齐发的请求打散。

【为什么需要】
13 个账户如果都在 09:35:00 齐刷刷向行情站点发请求，失败率会明显上升，
而取价失败在本系统里等于 price=0 拒单——表现为"策略今天莫名没开仓"。
随机延时 1~9 分钟把压力摊开。

【为什么要落日志】
调度器对单条命令有 10 分钟硬上限，超时直接 SIGKILL。睡眠占掉的时间越长，
真正跑完一轮的余量越小。所以睡前先记一行"计划"，睡醒补一行"已开跑"：
只有计划行、没有开跑行，就说明进程在睡眠中被杀掉了——
否则这种截断只会表现为"那一轮什么都没发生"，又是一次静默失效。

重构前这段逻辑在 run.py 和 run_exp.py 里各有一份，且两份不一致：
run.py 会落 jitter_log.csv 并支持 JITTER_MIN/MAX 环境变量，run_exp.py 都没有。
"""
from __future__ import annotations

import os
import random
import time
from datetime import datetime
from typing import Callable

from astock.runtime import paths
from astock.runtime.files import append_csv_row

JITTER_COLUMNS = ["唤醒时刻", "计划延时s", "实际开跑时刻", "实际延时s", "状态"]

DEFAULT_MIN_SEC = 60
DEFAULT_MAX_SEC = 540      # 9 分钟：为本轮约 18s 的实跑 + 重试留足余量


def bounds() -> tuple[int, int]:
    """从环境变量读延时区间，便于按账户错峰（A 组用更短的窗口）。"""
    lo = int(os.environ.get("JITTER_MIN", DEFAULT_MIN_SEC))
    hi = int(os.environ.get("JITTER_MAX", DEFAULT_MAX_SEC))
    return (lo, hi) if lo <= hi else (hi, lo)


def sleep_with_jitter(*, enabled: bool = True,
                      printer: Callable[[str], object] = print) -> int:
    """随机延时后返回实际睡了多少秒。enabled=False 时立即返回 0。"""
    if not enabled:
        return 0
    lo, hi = bounds()
    wait = random.randint(lo, hi)
    wake = datetime.now()
    printer(f"[jitter] 随机延时 {wait}s（{wait // 60}分{wait % 60}秒）后开跑，避开整点高峰…")

    log = paths.jitter_log()
    # 睡前落"计划"行：进程若在睡眠中被超时杀死，这行会孤零零地留下，
    # 与后面的 "fired" 行对不上，截断因此可被检出。
    append_csv_row(log, JITTER_COLUMNS, [wake.strftime("%H:%M:%S"), wait, "", "", "sleeping"])
    time.sleep(wait)

    fired = datetime.now()
    append_csv_row(log, JITTER_COLUMNS, [
        wake.strftime("%H:%M:%S"), wait, fired.strftime("%H:%M:%S"),
        round((fired - wake).total_seconds(), 1), "fired",
    ])
    return wait
