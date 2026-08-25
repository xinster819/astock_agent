"""analytics · 绩效统计与「能不能下结论」的判据（**纯函数，零 IO**）。

【为什么这一层比图表更重要】
这个项目的全部价值是**对照实验**：回答「规则决策 vs Agent 决策」「不同信号族之间」
孰优。而实测数据长这样：

    exp5   42 轮   12 笔平仓   −12.48%
    D 组   20 轮    0 笔平仓   + 1.55%

一个普通排行榜会把 D 组放在第一、exp5 放在最后，读的人自然得出
「新闻情绪策略胜出」。**这个结论毫无支撑**：D 组一笔都没平过仓（那 +1.55%
全是浮盈），而且只跑了 20 轮，exp5 跑了 42 轮——两者连观察期都不一样长。

所以本模块的核心不是「算出收益率」，而是**先回答能不能比**。
排名之前必须先过三关：样本量、轮次可比性、闸门。任何一关不过，
排名就只是好看的噪音。

这和项目其余部分是同一条原则的延伸：**宁可吵，也不沉默**。
一个把噪音渲染成结论的看板，比没有看板更糟——它会让人据此改策略。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# 样本量分级
# ---------------------------------------------------------------------------

#: 平仓笔数低于此值，任何收益差异都不该被解读。
MIN_TRADES_FOR_SIGNAL = 10
#: 平仓笔数达到此值才允许进入初步比较。
MIN_TRADES_FOR_COMPARISON = 30
#: 各账户轮次数的极差超过均值的这个比例，就认为观察期不可比。
ROUND_SPREAD_TOLERANCE = 0.25

#: 双尾 95% 的 t 临界值，按自由度查表。
#: 为什么不用固定的 1.96/2：小样本下 t 分布的尾巴厚得多。n=3（df=2）时临界值是
#: 4.30，用 2 去判会把「3 笔全赢」判成「有显著优势」——这正是本模块要防的
#: 过早结论。纯 stdlib 实现，不为一个查表引入 scipy。
_T_CRITICAL_95 = {
    9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    24: 2.064, 29: 2.045, 39: 2.023, 59: 2.000, 119: 1.980,
}
_T_CRITICAL_LARGE = 1.960


def t_critical_95(degrees_of_freedom: int) -> float:
    """自由度对应的双尾 95% t 临界值。表外向上取最接近的一档（偏保守）。"""
    if degrees_of_freedom in _T_CRITICAL_95:
        return _T_CRITICAL_95[degrees_of_freedom]
    larger = [df for df in sorted(_T_CRITICAL_95) if df >= degrees_of_freedom]
    if larger:
        return _T_CRITICAL_95[larger[0]]
    return _T_CRITICAL_LARGE


class Tier(NamedTuple):
    """样本充分度分级。`rank_eligible` 决定它能否进入排名。"""

    key: str
    label: str
    rank_eligible: bool


TIER_INSUFFICIENT = Tier("insufficient", "样本不足", False)
TIER_INDICATIVE = Tier("indicative", "仅供观察", False)
TIER_COMPARABLE = Tier("comparable", "可初步比较", True)


def sample_tier(closed_trades: int) -> Tier:
    """按平仓笔数给出分级。

    分界值是刻意保守的：30 笔仍远不足以做统计推断，只是「值得看一眼」的下限。
    真要下结论需要的样本量比这大一个数量级——这一点会写在看板上，
    而不是藏在代码注释里。
    """
    if closed_trades < MIN_TRADES_FOR_SIGNAL:
        return TIER_INSUFFICIENT
    if closed_trades < MIN_TRADES_FOR_COMPARISON:
        return TIER_INDICATIVE
    return TIER_COMPARABLE


# ---------------------------------------------------------------------------
# 单账户绩效
# ---------------------------------------------------------------------------

@dataclass
class TradeStats:
    """已平仓交易的统计。全部基于 trades.csv 里的已实现盈亏。"""

    wins: int = 0
    losses: int = 0
    flats: int = 0
    gross_profit: float = 0.0        # 盈利笔的盈亏之和
    gross_loss: float = 0.0          # 亏损笔的盈亏之和（正数）
    pnls: list[float] = field(default_factory=list)

    @property
    def closed(self) -> int:
        return self.wins + self.losses + self.flats

    @property
    def win_rate(self) -> float | None:
        """胜率。没有平仓交易时返回 None，而不是 0%——0% 意味着「全亏」。"""
        decided = self.wins + self.losses
        return round(self.wins / decided * 100, 1) if decided else None

    @property
    def profit_factor(self) -> float | None:
        """盈亏比 = 总盈利 / 总亏损。>1 才是正期望。

        没有亏损笔时返回 None 而不是无穷大——「从没亏过」在 3 笔样本下
        是运气，报成 ∞ 会让它看起来像圣杯。
        """
        if self.gross_loss <= 0:
            return None
        return round(self.gross_profit / self.gross_loss, 2)

    @property
    def expectancy(self) -> float | None:
        """单笔期望盈亏（元）。这是「这套策略每做一笔平均赚多少」。"""
        return round(statistics.fmean(self.pnls), 2) if self.pnls else None

    @property
    def std_error(self) -> float | None:
        """单笔盈亏均值的标准误差。样本 <2 笔时无从谈起。"""
        if len(self.pnls) < 2:
            return None
        return statistics.stdev(self.pnls) / math.sqrt(len(self.pnls))

    @property
    def edge_is_detectable(self) -> bool | None:
        """单笔期望是否显著不为零。样本不足时返回 None（**不判**，而不是判否）。

        这是全看板最该被看见的一行。绝大多数账户在这里会是 False 或 None——
        意思是**它们的盈亏至今无法与随机区分**。

        两道保险，缺一不可：
          1. 平仓笔数须达到 `MIN_TRADES_FOR_SIGNAL`。低于此值一律不判——
             实测 exp6 用 3 笔全赢就能让 |均值| > 2×SE，那是纯粹的小样本假象。
          2. 用自由度对应的 t 临界值，不用固定的 2。df=2 时临界值是 4.30，
             拿 2 去判等于把置信度悄悄降到 80% 以下。
        """
        expectancy, se = self.expectancy, self.std_error
        if expectancy is None or se is None:
            return None
        if len(self.pnls) < MIN_TRADES_FOR_SIGNAL:
            return None
        # ⚠ se == 0（每笔盈亏完全相同）是**最可辨识**的情形，不是不可判——
        # 早期实现把它一并当作 None 返回，等于把「12 笔每笔都亏一千」
        # 判成了「看不出来」。下面的比较天然能处理：均值非零时 |均值| > 0 成立，
        # 均值恰为零时 0 > 0 不成立，两种都对。
        return abs(expectancy) > t_critical_95(len(self.pnls) - 1) * se


def trade_stats(trades: list[dict]) -> TradeStats:
    """从成交流水里统计已平仓交易。买入不计入——它没有已实现盈亏。"""
    stats = TradeStats()
    for trade in trades:
        pnl = trade.get("pnl")
        if pnl is None:
            continue
        stats.pnls.append(float(pnl))
        if pnl > 0:
            stats.wins += 1
            stats.gross_profit += pnl
        elif pnl < 0:
            stats.losses += 1
            stats.gross_loss += abs(pnl)
        else:
            stats.flats += 1
    stats.gross_profit = round(stats.gross_profit, 2)
    stats.gross_loss = round(stats.gross_loss, 2)
    return stats


# ---------------------------------------------------------------------------
# 权益曲线
# ---------------------------------------------------------------------------

class CurveStats(NamedTuple):
    """权益曲线派生的风险指标。空曲线时全部为 None——不拿 0 冒充。"""

    max_drawdown_pct: float | None      # 最大回撤（正数，%）
    peak_total: float | None
    trough_total: float | None
    observations: int
    span_days: int | None


def curve_stats(curve: list[dict]) -> CurveStats:
    """算最大回撤与观察跨度。

    回撤按**权益曲线的峰谷**算，不是按日收益——曲线是每轮写一行的，
    行距不均匀，用日收益会失真。
    """
    totals = [float(p["total"]) for p in curve if p.get("total") is not None]
    if not totals:
        return CurveStats(None, None, None, 0, None)

    peak = totals[0]
    max_dd = 0.0
    dd_peak = dd_trough = totals[0]
    for total in totals:
        peak = max(peak, total)
        if peak > 0:
            drawdown = (peak - total) / peak
            if drawdown > max_dd:
                max_dd, dd_peak, dd_trough = drawdown, peak, total

    span = _span_days(curve)
    return CurveStats(round(max_dd * 100, 2), round(dd_peak, 2),
                      round(dd_trough, 2), len(totals), span)


def _span_days(curve: list[dict]) -> int | None:
    from astock.reporting.metrics import parse_time

    times = [t for t in (parse_time(p.get("t", "")) for p in curve) if t]
    return (max(times) - min(times)).days if len(times) >= 2 else None


# ---------------------------------------------------------------------------
# 可比性判据
# ---------------------------------------------------------------------------

@dataclass
class Comparability:
    """整批账户能不能横向比较。`ok` 为 False 时，排名不该被当作结论。"""

    ok: bool
    headline: str
    reasons: list[str] = field(default_factory=list)
    eligible: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)


def comparability(accounts: list[dict]) -> Comparability:
    """三关：闸门、样本量、观察期。任一关不过就明说不可比。

    `accounts` 是已算好统计的账户 dict 列表（见 `summarise`）。
    """
    live = [a for a in accounts if a.get("exists")]
    if not live:
        return Comparability(False, "没有任何账户有数据。")

    reasons: list[str] = []
    eligible: list[str] = []
    excluded: list[str] = []

    for account in live:
        blockers = []
        if account.get("dirty"):
            blockers.append("账本未通过完整性对账")
        if account.get("stale"):
            blockers.append("数据过期")
        if not account["tier"]["rank_eligible"]:
            blockers.append(f"平仓仅 {account['closed_trades']} 笔")
        (excluded if blockers else eligible).append(account["account"])
        if blockers:
            account["exclusion_reasons"] = blockers

    # ---- 关卡一：闸门 ----
    dirty = [a["account"] for a in live if a.get("dirty")]
    stale = [a["account"] for a in live if a.get("stale")]
    if dirty:
        reasons.append(f"{len(dirty)} 个账户账本未通过对账（{', '.join(dirty)}），"
                       f"其净值不可用于排名。")
    if stale:
        reasons.append(f"{len(stale)} 个账户数据过期（{', '.join(stale)}）。")

    # ---- 关卡二：样本量 ----
    thin = [a for a in live if not a["tier"]["rank_eligible"]]
    if thin:
        worst = min(a["closed_trades"] for a in live)
        reasons.append(
            f"{len(thin)}/{len(live)} 个账户平仓笔数不足 {MIN_TRADES_FOR_COMPARISON}"
            f"（最少的只有 {worst} 笔）。样本这么小时，收益排名基本是噪音。")

    # ---- 关卡三：观察期是否可比 ----
    rounds = [a.get("round", 0) for a in live]
    if rounds and statistics.fmean(rounds) > 0:
        spread = (max(rounds) - min(rounds)) / statistics.fmean(rounds)
        if spread > ROUND_SPREAD_TOLERANCE:
            reasons.append(
                f"各账户运行轮次相差悬殊（{min(rounds)} ~ {max(rounds)} 轮）。"
                f"累计收益直接对比等于拿不同长度的观察期比大小。")

    if not reasons:
        return Comparability(True, f"{len(eligible)} 个账户通过全部前置检查，可以横向比较。",
                             eligible=eligible, excluded=excluded)

    # 文案里不带图标——⚠/✔ 由展示层给，否则换个渲染方式就会出现两个符号。
    headline = (f"现在还不能下结论：{len(eligible)}/{len(live)} 个账户满足比较条件。"
                if eligible else "现在还不能下结论：没有账户满足比较条件。")
    return Comparability(False, headline, reasons, eligible, excluded)


# ---------------------------------------------------------------------------
# 持仓重叠
# ---------------------------------------------------------------------------

def holding_overlap(accounts: list[dict]) -> list[dict]:
    """哪些票被多个账户同时持有，按持有账户数降序。

    这是「这些实验到底独不独立」的直接证据：如果 13 个账户大量持有同一批票，
    它们的收益就是同涨同跌，对照实验的信息量会远低于账户数暗示的那样。
    """
    holdings: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if not account.get("exists"):
            continue
        for position in account.get("positions", []):
            entry = holdings.setdefault(position["code"], {
                "code": position["code"], "name": position.get("name", ""),
                "held_by": [], "total_mv": 0.0,
            })
            entry["held_by"].append(account["account"])
            entry["total_mv"] += float(position.get("mv") or 0)

    for entry in holdings.values():
        entry["total_mv"] = round(entry["total_mv"], 2)
    return sorted(holdings.values(),
                  key=lambda h: (len(h["held_by"]), h["total_mv"]), reverse=True)


def concentration(account: dict) -> float | None:
    """最大单票市值占总资产的比例（%）。仓位集中度的粗略刻度。"""
    positions = account.get("positions") or []
    total = float(account.get("total") or 0)
    if not positions or total <= 0:
        return None
    return round(max(float(p.get("mv") or 0) for p in positions) / total * 100, 1)
