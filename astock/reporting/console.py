"""console · 对照实验观察台的数据装配。

【它和 dashboard.collect 的分工】
`dashboard.collect()` 负责**读账本**：把 13 个账户的 state / trades / equity
读成结构化数据。本模块负责**下结论**：在那份数据之上算绩效统计、
判断能不能横向比较、汇总系统健康度，产出观察台唯一的 JSON 负载。

两层分开是刻意的：读账本要碰文件与网络，下结论是纯计算。
所有「结论」都在 Python 侧算完再传给前端——前端只负责画，不负责判断。
这样每一个会被人当作结论的数字都是可单测的。

【观察台要回答的五个问题（按重要性排序）】
  1. 现在能不能下结论？        → verdict
  2. 如果能，谁在赢、赢多少？   → accounts 排名 + benchmark
  3. 赢在哪？                  → 胜率 / 盈亏比 / 单笔期望 / 回撤
  4. 系统健康吗？              → health
  5. 这些实验真的独立吗？       → overlap

第 1 条排在最前，是因为实测数据里几乎没有一组的样本量支持任何结论——
把噪音渲染成排行榜，比不做看板更糟。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from astock.reporting import analytics, dashboard, roster
from astock.runtime import clock, paths

#: 观察台上默认对比的基准指数
BENCHMARK = ("沪深300", "000300")


def build(use_live: bool = True) -> dict[str, Any]:
    """装配观察台的完整数据负载。"""
    raw, live_status = dashboard.collect(use_live=use_live)
    accounts = [_enrich(account) for account in raw]

    verdict = analytics.comparability(accounts)
    return {
        "meta": _meta(live_status, use_live),
        "verdict": {
            "ok": verdict.ok,
            "headline": verdict.headline,
            "reasons": verdict.reasons,
            "eligible": verdict.eligible,
            "excluded": verdict.excluded,
        },
        "thresholds": {
            "min_trades_for_signal": analytics.MIN_TRADES_FOR_SIGNAL,
            "min_trades_for_comparison": analytics.MIN_TRADES_FOR_COMPARISON,
        },
        "health": _health(accounts),
        "accounts": accounts,
        "overlap": analytics.holding_overlap(accounts),
        "benchmark": _benchmark(use_live),
    }


# ---------------------------------------------------------------------------

def _layer(entry: roster.ReportAccount | None) -> str:
    """账户分层。对照基线、规则实验、Agent 决策——看板按这个分组。"""
    if entry is None:
        return "rule"
    if entry.is_control:
        return "control"
    return "agent" if entry.is_agent else "rule"


def _enrich(account: dict[str, Any]) -> dict[str, Any]:
    """给一个账户补上绩效统计与样本分级。"""
    account = dict(account)
    entry = roster.by_account().get(account["account"])
    account["layer"] = _layer(entry)
    # `name` 是完整标签（"exp5·纯动量"），周报靠它跨周对齐，不能动。
    # 但界面上账户 id 已经单独一列，再拼一次就成了 "exp5 · exp5·纯动量"。
    # 所以另给一个裸策略名，展示层用它。
    account["strategy"] = entry.name if entry else account["account"]

    if not account.get("exists"):
        account.update(closed_trades=0, tier=_tier_dict(analytics.TIER_INSUFFICIENT),
                       trade_stats=None, curve=None, concentration=None,
                       dirty=False, stale=False)
        return account

    stats = analytics.trade_stats(account.get("trades", []))
    curve = analytics.curve_stats(account.get("equity", []))

    account["closed_trades"] = stats.closed
    account["tier"] = _tier_dict(analytics.sample_tier(stats.closed))
    account["trade_stats"] = {
        "closed": stats.closed, "wins": stats.wins, "losses": stats.losses,
        "flats": stats.flats,
        "win_rate": stats.win_rate,
        "profit_factor": stats.profit_factor,
        "expectancy": stats.expectancy,
        "std_error": round(stats.std_error, 2) if stats.std_error else None,
        "edge_is_detectable": stats.edge_is_detectable,
        "gross_profit": stats.gross_profit,
        "gross_loss": stats.gross_loss,
    }
    account["curve"] = {
        "max_drawdown_pct": curve.max_drawdown_pct,
        "observations": curve.observations,
        "span_days": curve.span_days,
    }
    account["concentration"] = analytics.concentration(account)
    account.update(_gates(account))
    return account


def _tier_dict(tier: analytics.Tier) -> dict[str, Any]:
    return {"key": tier.key, "label": tier.label, "rank_eligible": tier.rank_eligible}


def _gates(account: dict[str, Any]) -> dict[str, Any]:
    """跑一遍账本完整性与引擎停摆判定。

    看板上的每一个收益数字，都必须先经过这两道闸门——
    脏账本算出来的收益率看起来和干净的一模一样。
    """
    from astock.guards import integrity
    from astock.runtime import files

    account_paths = paths.AccountPaths.for_account(account["account"])
    state = files.read_json(account_paths.state)
    if state is None:
        return {"dirty": False, "stale": False, "red_flags": []}

    trades = files.read_csv_rows(account_paths.trades)
    result = integrity.check(trades, state,
                             init_cash=state.get("init_cash", 1_000_000.0))
    return {
        "dirty": not result["clean"],
        "stale": False,          # 由 health 统一按停摆判定填充
        "red_flags": [{"check": f["check"], "severity": f["severity"],
                       "detail": f["detail"]} for f in result["red_flags"]],
    }


def _health(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """系统健康度：停摆、脏账、行情降级。

    停摆检测走 `guards.freshness.find_stalled`——引擎在跑但从未进入下单分支，
    是这个项目最贵的一课，看板必须把它放在显眼位置。
    """
    from astock.guards import freshness

    stalled = freshness.find_stalled()
    for account in accounts:
        account["stale"] = account["account"] in stalled

    dirty = [a["account"] for a in accounts if a.get("dirty")]
    idle = [a["account"] for a in accounts
            if a.get("exists") and a.get("closed_trades", 0) == 0]

    return {
        "stalled": stalled,
        "dirty": dirty,
        "never_closed": idle,
        "all_clear": not stalled and not dirty,
        "note": ("全部账户账本自洽、引擎均进入过下单分支。" if not stalled and not dirty
                 else "存在需要处理的问题，相关账户的净值不可用于排名。"),
    }


def _meta(live_status: str, use_live: bool) -> dict[str, Any]:
    from astock.guards import regime as regime_mod

    try:
        result = regime_mod.classify() if use_live else None
    except Exception:
        result = None

    return {
        "generated_at": clock.stamp(),
        "live_status": live_status,
        "workspace": str(paths.workspace()),
        "regime": None if result is None else {
            "regime": result.regime, "source": result.source,
            "degraded": bool(result.degraded), "detail": result.detail,
        },
    }


def _benchmark(use_live: bool) -> dict[str, Any] | None:
    """基准指数曲线。「跑赢大盘了吗」是看到排名后的第一个问题。

    取数失败返回 None——**绝不用任何方式伪造一条基准线**。
    没有基准时前端会明说「未取到基准」，而不是画一条平的假线。
    """
    if not use_live:
        return None
    name, code = BENCHMARK
    try:
        import datetime as dt

        from astock.data import market

        end = dt.datetime.now().strftime("%Y%m%d")
        start = (dt.datetime.now() - dt.timedelta(days=180)).strftime("%Y%m%d")
        frame = market.get_index_hist(code, start, end)
        if frame is None or len(frame) < 2:
            return None
        points = [{"t": str(row["日期"]), "close": float(row["收盘"])}
                  for _, row in frame.iterrows()]
        return {"name": name, "code": code, "points": points}
    except Exception as exc:
        return {"name": name, "code": code, "points": [],
                "error": f"{exc!r}"[:120]}


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"


def render(payload: dict[str, Any], out_path: Path | None = None) -> Path:
    """把负载与前端资源装配成**单个自包含 HTML 文件**，返回输出路径。

    为什么是单文件而不是起个服务：项目的硬约束之一是「禁止启动网络监听进程」
    （README 边界，`exp_scheduler` 与 `market` 两处各自重申过）。
    观察台是只读的，数据源本来就是定时任务产出的 CSV——没有任何需要
    实时推送的东西。生成一个双击就能开、断网也能看的文件，
    比起一个必须先启动、还占着端口的服务，对这个场景是更好的答案。

    CSS/JS 内联的另一个好处：可以直接把文件发给别人看，不需要带一堆附件。
    """
    template = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
    css = (WEBAPP_DIR / "console.css").read_text(encoding="utf-8")
    js = (WEBAPP_DIR / "console.js").read_text(encoding="utf-8")

    html = (template
            .replace("__CSS__", css)
            .replace("__JS__", js)
            .replace("__PAYLOAD__", _embed(payload)))

    out_path = out_path or paths.dashboard_html()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _embed(payload: dict[str, Any]) -> str:
    """把负载序列化进 `<script type="application/json">`。

    `</script>` 一旦出现在数据里就会提前闭合脚本块，页面从此错乱——
    而数据里确实有 agent 写的自由文本（成交备注）。转义 `<` 是必须的。
    """
    import json

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c")
