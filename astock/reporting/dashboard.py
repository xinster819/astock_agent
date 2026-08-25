"""
观测站生成器：读取全部账户(A组主账户 / exp1-8 / B组)的 state+equity+trades，
生成时拉一次实时行情算个股浮盈，渲染成一个【自包含单页 HTML】(数据内嵌 + Chart.js)，
供随时打开观察各组炒股进展、持仓、交易记录、整体收益与个股收益。

设计：
  - 沙箱禁止常驻网络服务，故产物是静态 HTML，不依赖后端，双击即可看。
  - 数据每天变 -> 本脚本可随时重跑刷新；也可挂定时任务自动重生成并上传。
  - 个股收益 = 已实现盈亏(卖出记录备注里的"盈亏")+ 未实现浮盈(现价 vs 成本)。
用法：python3 dashboard.py            # 生成 dashboard.html
      python3 dashboard.py --no-live  # 跳过实时行情(用成本价占位，快速出图)
"""

from astock.reporting import metrics, roster
from astock.runtime import files

#: 每个账户在曲线图上的颜色。**纯展示**，所以留在看板里，
#: 不进 roster——weekly 产出的是 JSON，不该被迫携带配色。
SERIES_COLORS = {
    "A":    "#64748b",
    "exp1": "#3b82f6", "exp2": "#22c55e", "exp3": "#f59e0b",
    "exp4": "#ef4444", "exp5": "#a855f7", "exp6": "#06b6d4",
    "exp7": "#84cc16", "exp8": "#f97316", "exp9": "#14b8a6",
    "B":    "#ec4899", "C": "#8b5cf6", "D": "#0ea5e9",
}
FALLBACK_COLOR = "#94a3b8"


def collect_live_prices(need_codes, use_live=True):
    """一次性拉取所有需要的代码的实时现价。返回 ({code:{'price','name'}}, status)。
    非交易时段/取价失败会返回空价，调用方回退到成本价。"""
    prices = {}
    if not use_live or not need_codes:
        return prices, "未取实时价(--no-live)"
    try:
        from astock.data import market
        quotes = market.get_quotes(sorted(need_codes))
        for c, q in quotes.items():
            px = q.get("price") or 0
            if px and px > 0 and not q.get("error"):
                prices[c] = {"price": float(px), "name": q.get("name", "")}
        status = market.is_trading_now()[1]
        return prices, f"实时({status}，取得{len(prices)}/{len(need_codes)}只)"
    except Exception as e:
        return prices, f"行情不可用({repr(e)[:40]})"


def collect(use_live=True):
    # 先汇总所有账户的持仓代码，一次性取价（避免每组重复请求）
    need_codes = set()
    raw = []
    for account in roster.roster():
        st = files.read_json(account.paths.state)
        eq = files.read_csv_rows(account.paths.equity)
        tr = files.read_csv_rows(account.paths.trades)
        raw.append((account, st, eq, tr))
        if st:
            need_codes |= set(st.get("positions", {}).keys())

    live, live_status = collect_live_prices(need_codes, use_live)

    data = []
    for account, st, eq, tr in raw:
        name = account.label
        desc = account.desc
        color = SERIES_COLORS.get(account.account, FALLBACK_COLOR)
        if st is None:
            data.append({
                "account": account.account,
                "name": name, "desc": desc, "color": color,
                "exists": False, "init": 1_000_000.0, "cash": 1_000_000.0,
                "mv": 0.0, "total": 1_000_000.0, "ret": 0.0, "round": 0,
                "positions": [], "equity": [], "trades": [],
                "realized": 0.0, "unrealized": 0.0, "win": 0, "loss": 0,
            })
            continue

        init = st.get("init_cash", 1_000_000.0)
        cash = st.get("cash", init)
        pos = st.get("positions", {})

        # ---- 持仓 + 个股未实现浮盈（现价优先，回退成本） ----
        positions = []
        mv_live = 0.0
        for code, p in pos.items():
            qty = p.get("qty", 0)
            cost = p.get("cost", 0)
            lp = live.get(code, {})
            price = lp.get("price") or cost  # 无实时价则用成本(=0浮盈)
            pos_mv = price * qty
            mv_live += pos_mv
            upnl = (price - cost) * qty
            upnl_pct = (price / cost - 1) * 100 if cost else 0.0
            positions.append({
                "code": code, "name": p.get("name", "") or lp.get("name", ""),
                "qty": qty, "available": p.get("available", 0),
                "cost": round(cost, 3), "price": round(price, 3),
                "mv": round(pos_mv, 2),
                "upnl": round(upnl, 2), "upnl_pct": round(upnl_pct, 2),
                "live": bool(lp.get("price")),
            })
        positions.sort(key=lambda x: x["upnl"], reverse=True)

        # 总资产：优先用实时持仓市值；无实时价时回退 equity 末行/成本
        mv_cost = sum(p.get("qty", 0) * p.get("cost", 0) for p in pos.values())
        mv = mv_live if live else mv_cost
        total = cash + mv
        if not live and eq:
            try:
                last = eq[-1]
                mv = metrics.to_float(last.get("持仓市值"), mv_cost)
                total = metrics.to_float(last.get("总资产"), cash + mv_cost)
            except Exception:
                pass
        ret = (total / init - 1) * 100 if init else 0.0
        unrealized = sum(p["upnl"] for p in positions)

        # ---- 已实现盈亏 + 胜负（从卖出记录备注解析） ----
        realized = 0.0
        win = loss = 0
        for r in tr:
            if "卖" not in r.get("方向", ""):
                continue
            pnl = metrics.extract_realized_pnl(r.get("备注", ""))
            if pnl is None:
                continue
            realized += pnl
            if pnl > 0:
                win += 1
            elif pnl < 0:
                loss += 1
        realized = round(realized, 2)

        # ---- equity 曲线点 ----
        ecurve = []
        for r in eq:
            try:
                ecurve.append({"t": r.get("时间", ""),
                               "total": metrics.to_float(r.get("总资产")),
                               "ret": metrics.to_float(r.get("累计收益率%"))})
            except Exception:
                continue

        # ---- 最近交易(最多30笔，倒序) ----
        trades = []
        for r in tr[-30:][::-1]:
            note = r.get("备注", "")
            trades.append({
                "t": r.get("时间", ""), "side": r.get("方向", ""),
                "code": r.get("代码", ""), "name": r.get("名称", ""),
                "price": r.get("价格", ""), "qty": r.get("数量", ""),
                "amount": r.get("成交额", ""),
                "pnl": metrics.extract_realized_pnl(note),
                "note": note,
            })

        data.append({
            "account": account.account,
            "name": name, "desc": desc, "color": color, "exists": True,
            "init": init, "cash": round(cash, 2), "mv": round(mv, 2),
            "total": round(total, 2), "ret": round(ret, 3),
            "round": st.get("round", 0),
            "realized": realized, "unrealized": round(unrealized, 2),
            "win": win, "loss": loss,
            "positions": positions, "equity": ecurve, "trades": trades,
        })
    return data, live_status

# 渲染已移交 `console` —— 观察台的前端资源是 webapp/ 下的真实 html/css/js 文件，
# 不再是一段嵌在 .py 里的 300 行模板字符串（那种写法没有语法高亮、没有静态检查、
# 改起来也搜不到）。本模块从此只管【读账本】。
