"""metrics · 报表的算术（**纯函数，零 IO**）。

【为什么单独成模块】
周报里的每一个数字都是对照实验的**结论**：某组本周涨了多少、赢了几笔、
跑赢基准没有。这些数字算错，比报表干脆出不来危险得多——错的数字看起来
和对的一模一样，而整个项目就是靠它们来判断"规则决策 vs Agent 决策"孰优。

重构前这些算术埋在 `weekly.collect()` 那个 160 行函数中间，与账本读取、
闸门调用、HTML 组装缠在一起，因此一行都没有测试。拆出来之后，
每个函数的输入输出都是普通数据，可以逐条钉死边界行为。

【本模块的一条硬规矩】
取不到数据一律返回 `None`，**绝不返回 0**。0 是一个合法的收益率，
而"没有数据"不是——把二者混同，就等于把数据缺失伪装成了"本周持平"。
项目的第一条边界写着"不伪造、不回填净值"，这里是它在算术层的落点。
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, NamedTuple

#: 卖出备注里形如 "盈亏-8733.24" / "盈亏 123.4" 的已实现盈亏
PNL_PATTERN = re.compile(r"盈亏\s*([+-]?\d+(?:\.\d+)?)")

#: 账本时间列可能的两种格式。只认这两种，认不出就是 None——
#: 悄悄猜一个日期出来会让整周的成交被划进错误的窗口。
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def to_float(value: Any, default: float | None = 0.0) -> Any:
    """宽松转 float。账本是 CSV，列里什么都可能有。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_time(value: Any) -> dt.datetime | None:
    """解析账本时间列。无法解析返回 None，不猜。"""
    if isinstance(value, dt.datetime):
        return value
    if not isinstance(value, str):
        return None
    for fmt in _TIME_FORMATS:
        try:
            return dt.datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def extract_realized_pnl(note: Any) -> float | None:
    """从成交备注里取出已实现盈亏。买入没有盈亏，返回 None。

    ⚠ 返回 None 与返回 0.0 语义完全不同：前者是"这笔不是平仓"，
    后者是"平了但不赚不亏"。胜负统计要能分辨。
    """
    if not isinstance(note, str):
        return None
    match = PNL_PATTERN.search(note)
    return round(float(match.group(1)), 2) if match else None


# ---------------------------------------------------------------------------
# 周窗口
# ---------------------------------------------------------------------------

class WeekWindow(NamedTuple):
    """一个 ISO 周的时间边界。"""

    start: dt.datetime        # 本周一 00:00:00
    end: dt.datetime          # 本周结束：now 与本周日 23:59:59 取较早者
    prev_start: dt.datetime   # 上周一 00:00:00
    prev_end: dt.datetime     # 上周日 23:59:59
    iso: str                  # "2026-W34"


def week_bounds(week_str: str | None = None,
                now: dt.datetime | None = None) -> WeekWindow:
    """算出周边界。`week_str` 形如 `2026-W34`，省略则取 now 所在周。

    `end` 取 min(now, 周日 23:59:59)：周中跑周报时，窗口不该延伸到未来——
    否则"本周至今"会被当成"整周"，与上一周的完整值直接对比是不公平的。
    """
    now = now or dt.datetime.now()
    if week_str:
        year, week = week_str.upper().split("-W")
        monday = dt.datetime.strptime(f"{year}-W{int(week):02d}-1", "%G-W%V-%u")
    else:
        monday = (now - dt.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)

    return WeekWindow(
        start=monday,
        end=min(now, monday + dt.timedelta(days=6, hours=23, minutes=59, seconds=59)),
        prev_start=monday - dt.timedelta(days=7),
        prev_end=monday - dt.timedelta(seconds=1),
        iso=monday.strftime("%G-W%V"),
    )


# ---------------------------------------------------------------------------
# 权益曲线取点
# ---------------------------------------------------------------------------

class EquityPoint(NamedTuple):
    at: dt.datetime
    total: float
    cumulative_return_pct: float


def _point(row: dict) -> EquityPoint | None:
    at = parse_time(row.get("时间", ""))
    if at is None:
        return None
    return EquityPoint(at, to_float(row.get("总资产")), to_float(row.get("累计收益率%")))


def last_point_before(curve: list[dict], deadline: dt.datetime) -> EquityPoint | None:
    """曲线里 <= deadline 的最后一个点。用作区间终值。"""
    candidates = [p for p in map(_point, curve) if p and p.at <= deadline]
    return max(candidates, key=lambda p: p.at) if candidates else None


def first_point_after(curve: list[dict], start: dt.datetime) -> EquityPoint | None:
    """曲线里 >= start 的第一个点。上周末无点时用它当本周起点。"""
    candidates = [p for p in map(_point, curve) if p and p.at >= start]
    return min(candidates, key=lambda p: p.at) if candidates else None


class WeekReturn(NamedTuple):
    """本周收益。取不到起点或终点时两项都是 None——不拿 0 冒充。"""

    pnl: float | None
    pct: float | None


def week_return(curve: list[dict], window: WeekWindow) -> WeekReturn:
    """算本周的绝对盈亏与百分比收益。

    起点优先取**上周末最后一个点**，而不是本周第一个点：账户在周一开盘前
    就已经持有仓位，用本周首个观测当起点会把周末的跳空算丢。
    上周完全没有观测（新账户）时才退回本周首点。
    """
    end = last_point_before(curve, window.end)
    start = last_point_before(curve, window.prev_end) or first_point_after(curve, window.start)
    if not (end and start and start.total):
        return WeekReturn(None, None)
    return WeekReturn(
        pnl=round(end.total - start.total, 2),
        pct=round((end.total / start.total - 1) * 100, 3),
    )


# ---------------------------------------------------------------------------
# 成交统计
# ---------------------------------------------------------------------------

class TradeStats(NamedTuple):
    """区间内的成交统计。`wins + losses` 未必等于 `len(trades)`——
    买入不产生已实现盈亏，平推（盈亏恰为 0）也不计入任何一边。"""

    trades: list[dict]
    realized: float
    wins: int
    losses: int

    @property
    def closed(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        """胜率。没有平仓交易时返回 None，而不是 0%。"""
        return round(self.wins / self.closed * 100, 1) if self.closed else None


def trades_in_window(rows: list[dict], window: WeekWindow) -> TradeStats:
    """筛出落在窗口内的成交并统计已实现盈亏与胜负。"""
    selected: list[dict] = []
    realized = 0.0
    wins = losses = 0

    for row in rows:
        at = parse_time(row.get("时间", ""))
        if not (at and window.start <= at <= window.end):
            continue
        pnl = extract_realized_pnl(row.get("备注", ""))
        if pnl is not None:
            realized += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        selected.append({
            "t": row.get("时间", ""), "side": row.get("方向", ""),
            "code": row.get("代码", ""), "name": row.get("名称", ""),
            "price": row.get("价格", ""), "qty": row.get("数量", ""),
            "amount": row.get("成交额", ""), "pnl": pnl,
            "note": row.get("备注", ""),
        })

    return TradeStats(selected, round(realized, 2), wins, losses)
