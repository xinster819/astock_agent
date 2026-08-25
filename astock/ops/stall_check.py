"""stall_check · 引擎停摆自检。2026-07-31 事故后加的硬性收尾动作。

【为什么必须每轮跑】
那次事故里，进程照常运行、权益曲线照常写、账本完整性全绿、报告按时产出，
唯独 13 个账户里有 12 个连续三周没进入下单分支。**全套闸门无一告警。**
所以每轮结束都要主动问一句："真的有账户在交易吗？"

判据用 `round` / `last_trading_round_date` 而不是"零成交"——
高门槛策略长期无信号是正常的，长期**不进下单分支**才是故障。

【为什么从 shell 里搬进来】
这段逻辑原先是 scheduler_tick.sh 里的一段内联 Python heredoc。嵌在 shell
字符串里的代码没有测试、没有静态检查、重构时也搜不到——本次分包就把它
里面的 `import market_time` 打断了，而它恰恰是用来发现"东西悄悄坏了"的那段代码。
一个自己会静默失效的停摆检测器，比没有更糟。
"""
from __future__ import annotations

from astock.guards import freshness
from astock.runtime import clock

#: 判定本身在 `guards.freshness`——「引擎有没有停摆」是闸门判定，不是运维脚本。
#: 本模块只剩打印外壳。
REVIEW_DAYS = freshness.STALL_REVIEW_DAYS

find_stalled = freshness.find_stalled


def report(printer=print) -> int:
    """打印停摆自检结果。返回停摆账户数，供调用方决定退出码。"""
    clock.enforce()
    stalled = find_stalled()
    if stalled:
        printer(f"🔴 停摆账户 {len(stalled)}/13: {', '.join(stalled)}")
        printer("   这些账户在跑但从未进入下单分支——检查交易时段判定与进程时区。")
    else:
        printer("✅ 停摆自检：13 个账户全部正常进入过下单分支")
    return len(stalled)
