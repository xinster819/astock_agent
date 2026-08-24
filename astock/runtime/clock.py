"""
market_time · 交易所时钟（单一事实源）
=====================================================================
系统里一切"现在几点""今天几号"都应该以交易所时区(Asia/Shanghai)为准，
不再隐式依赖进程本地时区。

【为什么有这个模块 —— 2026-07-31 停摆事故】
  原调度平台的 16 个任务 meta_info.timezone 全部是 Asia/Shanghai，cron 也
  是北京时间，调度层从来没错。错的是 agent 回合里 Bash 进程拿到的环境时区：
  A 组进程是 Asia/Shanghai，exp*/B/C/D 进程是 UTC。而 is_trading_now() 用裸
  dt.datetime.now() 去比 9:30-11:30 / 13:00-15:00 —— 同一个北京时间 14:00，
  UTC 进程读成 06:00，判定"未开盘"，直接走空跑分支。
  后果：2026-07-31 ~ 08-21 连续三周，13 个账户里 12 个 round 冻结、零成交，
  而权益曲线照常写、账本照常自洽，全套闸门无一告警。

【两层防御，缺一不可】
  第一层 enforce()：入口脚本把进程 TZ 钉死为 Asia/Shanghai。
      覆盖面最广 —— 所有裸 datetime.now() 产出的账本时间戳与日期标签
      (_today() / risk_date / last_settle_date / trades.csv 时间列) 一并正确。
      这一层是必需的，不是"双保险"：仅修 is_trading_now 管不到这些日期标签。
      实测：进程 TZ = America/Los_Angeles 时（本次迁移目标机器就是），北京
      08-25 09:30~14:00 的裸本地时间是 08-24 18:30~23:00，账本日期会整体
      错位一天，weekly_collect 的周边界跟着错。

  第二层 to_market()：显式时钟。即使某个新入口忘了调 enforce()，
      交易时段判定也不会错 —— is_trading_now 走这一层兜底。

A 股无夏令时，ZoneInfo 不可用时退回固定 UTC+8，语义完全等价。
纯 stdlib。
"""
import datetime as dt
import os
import time

MARKET_TZ_NAME = "Asia/Shanghai"
_FIXED_UTC8 = dt.timezone(dt.timedelta(hours=8), MARKET_TZ_NAME)

try:                                    # 3.9+ 标准库；缺 tzdata 的最小环境会失败
    from zoneinfo import ZoneInfo
    MARKET_TZ = ZoneInfo(MARKET_TZ_NAME)
except Exception:                       # A股无夏令时，固定 +8 与 IANA 定义等价
    MARKET_TZ = _FIXED_UTC8


def enforce():
    """把进程本地时区钉死为交易所时区。入口脚本应在最早处调用。

    幂等、可重复调用。返回 True 表示进程本地时区确已生效为 UTC+8。
    Windows 无 time.tzset()，此时返回 False —— 由 to_market() 兜底，
    但账本日期标签仍会用系统时区，需在部署侧设好 TZ。
    """
    os.environ["TZ"] = MARKET_TZ_NAME
    if hasattr(time, "tzset"):
        time.tzset()
    return offset_ok()


def offset_ok():
    """进程本地时区当前是否等于 UTC+8。用于启动自检。"""
    return dt.datetime.now().astimezone().utcoffset() == dt.timedelta(hours=8)


def verify(printer=print):
    """启动自检：进程时区不对就大声说出来，绝不静默降级。

    这次事故的教训是"静默失效"——闸门全绿、报告照出，唯独引擎没转。
    所以这里宁可吵，也不沉默。返回 True/False。
    """
    if offset_ok():
        return True
    local = dt.datetime.now().astimezone().utcoffset()
    printer(
        f"🔴 进程本地时区不是 UTC+8（当前偏移 {local}）。"
        f"交易时段判定已由 market_time 显式兜底，但账本日期标签"
        f"(trades/equity 时间列、last_settle_date、risk_date) 仍会按本地时区落盘，"
        f"可能整体错位。请在部署侧设置 TZ={MARKET_TZ_NAME}。"
    )
    return False


def to_market(value=None):
    """把任意 datetime 归一到交易所时区，返回带时区的 datetime。

    value 为 None -> 取当前时刻。
    value 为朴素 datetime -> 按【进程本地时区】解释后转换（Python 语义）。
      这正是我们要的：调用方传进来的 dt.datetime.now() 就是进程本地时间。
    value 已带时区 -> 直接换算。
    """
    if value is None:
        return dt.datetime.now(MARKET_TZ)
    return value.astimezone(MARKET_TZ)


def now():
    """带时区的"现在"（交易所时区）。"""
    return dt.datetime.now(MARKET_TZ)


def naive_now():
    """交易所墙上时间，去掉 tzinfo。

    ⚠ 只用于生成【展示/落盘的字符串】。不要拿它做 .timestamp() 或与进程本地
    朴素时间做算术 —— 进程 TZ 不是北京时时会整体偏移。需要单调/时间戳语义
    时请直接用 dt.datetime.now()（enforce() 之后它就是北京时间）。
    """
    return now().replace(tzinfo=None)


def today():
    """交易所日期 YYYY-MM-DD。"""
    return now().strftime("%Y-%m-%d")


def stamp():
    """交易所时间戳 YYYY-MM-DD HH:MM:SS。"""
    return now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    print("== 交易所时钟体检 ==")
    print(f"  tz 实现        : {MARKET_TZ!r}")
    print(f"  enforce() 前   : 本地 {dt.datetime.now()}  偏移 {dt.datetime.now().astimezone().utcoffset()}")
    ok = enforce()
    print(f"  enforce() 后   : 本地 {dt.datetime.now()}  偏移 {dt.datetime.now().astimezone().utcoffset()}")
    print(f"  交易所时间     : {stamp()}")
    print(f"  进程时区达标   : {ok}")
