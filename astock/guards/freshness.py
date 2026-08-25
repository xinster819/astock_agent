"""Deterministic freshness checks for weekly equity data.

This module deliberately does not alter collection or normalize persisted data.  It
accepts the row dictionaries emitted by ``weekly_collect`` as well as common
legacy spellings for the timestamp field.
"""
import datetime as dt

_FRESHNESS_TOLERANCE_TRADING_DAYS = 1
# 引擎停摆容忍度：连续 2 个交易日没有跑完任何下单轮次即判定停摆。
# 取 2 而非 1，是为了容忍单日调度失败/超时截断这类偶发抖动。
_STALL_TOLERANCE_TRADING_DAYS = 2
_TIME_KEYS = ("时间", "timestamp", "datetime", "date", "ts", "time")


def _parse_time(value):
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(text, fmt)
            except ValueError:
                pass
    return None


def _row_time(row):
    if isinstance(row, dict):
        for key in _TIME_KEYS:
            parsed = _parse_time(row.get(key))
            if parsed is not None:
                return parsed
    elif isinstance(row, (tuple, list)) and row:
        return _parse_time(row[0])
    return _parse_time(row)


def _trading_days_between(start, end):
    """Count weekdays strictly after start's date through end's date."""
    if end.date() <= start.date():
        return 0
    day = start.date() + dt.timedelta(days=1)
    last = end.date()
    count = 0
    while day <= last:
        if day.weekday() < 5:
            count += 1
        day += dt.timedelta(days=1)
    return count


def _flag(check, detail, severity="error"):
    return {"check": check, "severity": severity, "detail": detail}


def check(state, equity_rows, now=None, review_start=None, review_end=None):
    """Return whether equity data is sufficiently fresh for a review.

    ``review_end`` is the intended end of the review window.  When omitted,
    ``now`` is used.  A latest point up to one trading day behind the effective
    end is accepted, which covers normal post-close/weekend collection timing.
    """
    state = state or {}
    rows = list(equity_rows or [])
    now = _parse_time(now) or dt.datetime.now()
    end = _parse_time(review_end) or now
    effective_end = min(end, now)
    start = _parse_time(review_start)
    if start is None:
        start = effective_end - dt.timedelta(days=effective_end.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)

    flags = []
    points = [point for point in (_row_time(row) for row in rows) if point is not None]
    if not points:
        flags.append(_flag("missing_equity", "未找到任何可解析的权益点"))
        return {"fresh": False, "red_flags": flags}

    latest = max(points)
    age = _trading_days_between(latest, effective_end)
    if age > _FRESHNESS_TOLERANCE_TRADING_DAYS:
        flags.append(_flag(
            "stale_equity",
            f"最新权益点 {latest:%Y-%m-%d %H:%M:%S} 落后复盘结束 {age} 个交易日",
        ))

    current_week_points = [point for point in points if start <= point <= effective_end]
    if not current_week_points:
        flags.append(_flag(
            "no_current_week_equity",
            f"复盘周 {start:%Y-%m-%d} 至 {effective_end:%Y-%m-%d} 没有权益点",
        ))

    # ---- 引擎停摆：进程在跑、权益在写，但从未进入下单分支 ----
    # 2026-07-31~08-21 事故的直接判据。当时 12 个账户 equity 每天照写(所以
    # stale_equity 不报)、账本自洽(所以 integrity_gate 全绿)，唯独 round 冻结、
    # 一笔单没下，全套闸门无一告警。这条就是补上那个盲区。
    #   last_trading_round_date —— 新字段，由 run/run_exp/execute 在下单分支写。
    #   risk_date              —— 老字段，同样只在下单分支写，作为历史账本的回退判据。
    # 两者都缺则保持沉默：无从判断，不误报（空 state 必须判 fresh）。
    stall_ref = state.get("last_trading_round_date") or state.get("risk_date")
    stall_at = _parse_time(stall_ref) if stall_ref else None
    if stall_at is not None:
        idle = _trading_days_between(stall_at, effective_end)
        if idle > _STALL_TOLERANCE_TRADING_DAYS:
            flags.append(_flag(
                "stalled_engine",
                f"最后一次跑完下单轮次是 {stall_at:%Y-%m-%d}，距复盘结束已 {idle} 个交易日"
                f"（容忍 {_STALL_TOLERANCE_TRADING_DAYS}）。权益仍在写但引擎未进入下单分支，"
                f"该账户本期净值仅为持仓 beta 漂移，不可用于策略评估。",
            ))

    if "previous_round" in state:
        previous = state.get("previous_round")
        current = state.get("round", state.get("current_round"))
        try:
            advancing = float(current) > float(previous)
        except (TypeError, ValueError):
            advancing = False
        if not advancing:
            flags.append(_flag(
                "non_advancing_round",
                f"当前 round={current!r} 未超过 previous_round={previous!r}",
            ))

    return {"fresh": not flags, "red_flags": flags}
