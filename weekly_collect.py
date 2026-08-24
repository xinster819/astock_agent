"""
周度复盘 · Layer 1 数据底座（确定性、可复现、零主观）
========================================================
读全部 10 个账户(A组主账户 / exp1-8 / B组)的 state+equity+trades，
算出"本周维度"的收益增量、归因原料、持仓明细，并抓大盘/板块指数作为 beta 基准，
产出一个纯数字 JSON —— 供上层分析 agent(Layer 3)做归因与前瞻。

设计原则：
  - 只出"事实"，不做判断。归因/红绿灯/前瞻是 agent 的活，本脚本不碰。
  - 周边界对齐交易日：本周 = 最近一个自然周(周一00:00 ~ 现在)，
    上周 = 再往前一个自然周。收益增量 = 本周末权益点 vs 上周末权益点。
  - 指数 beta 基准走腾讯源(市场源 eastmoney 对指数 secid 返回502，
    sina/tencent 用 sh/sz 前缀可正常取指数)，取不到就置 null，不阻塞。

用法：
  python3 weekly_collect.py                # 抓实时指数，产出 weekly_data_<ISO周>.json
  python3 weekly_collect.py --no-live      # 跳过指数抓取(离线/非交易时段快速出数据)
  python3 weekly_collect.py --week 2026-W27  # 指定复盘周(默认当前周)
"""
import os
import sys
import re
import json
import csv
import datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))

# 账户清单：(展示名, 策略说明, state, equity, trades)
ACCOUNTS = [
    ("A组·纯规则对照", "基准对照组(纯规则自动交易)",
     "state.json", "equity.csv", "trades.csv"),
    ("exp1·基准策略", "原始均线趋势跟踪",
     "experiments/exp1_state.json", "experiments/exp1_equity.csv", "experiments/exp1_trades.csv"),
    ("exp2·放宽买入", "降低买入门槛，更快入场",
     "experiments/exp2_state.json", "experiments/exp2_equity.csv", "experiments/exp2_trades.csv"),
    ("exp3·严格趋势", "只抓强趋势，提高胜率",
     "experiments/exp3_state.json", "experiments/exp3_equity.csv", "experiments/exp3_trades.csv"),
    ("exp4·金叉策略", "MA5上穿MA20金叉买入",
     "experiments/exp4_state.json", "experiments/exp4_equity.csv", "experiments/exp4_trades.csv"),
    ("exp5·纯动量", "不择时，只买动量最强的",
     "experiments/exp5_state.json", "experiments/exp5_equity.csv", "experiments/exp5_trades.csv"),
    ("exp6·状态适配趋势", "仅在正常市场环境运行的低仓位趋势策略",
     "experiments/exp6_state.json", "experiments/exp6_equity.csv", "experiments/exp6_trades.csv"),
    ("exp7·均值回归", "中期趋势内的超卖反弹策略",
     "experiments/exp7_state.json", "experiments/exp7_equity.csv", "experiments/exp7_trades.csv"),
    ("exp8·质量突破", "趋势、突破与成交量确认策略",
     "experiments/exp8_state.json", "experiments/exp8_equity.csv", "experiments/exp8_trades.csv"),
    ("exp9·多因子排序", "门槛入围后按合成因子分择强的横截面选股策略",
     "experiments/exp9_state.json", "experiments/exp9_equity.csv", "experiments/exp9_trades.csv"),
    ("B组·Agent决策", "规则做护栏，agent做最终买卖判断",
     "groupB/state.json", "groupB/equity.csv", "groupB/trades.csv"),
    ("C组·多空辩论", "规则做护栏，多智能体多空辩论后做最终买卖判断",
     "groupC/state.json", "groupC/equity.csv", "groupC/trades.csv"),
    ("D组·新闻情绪", "规则做护栏，结合新闻情绪面做最终买卖判断",
     "groupD/state.json", "groupD/equity.csv", "groupD/trades.csv"),
]

# 大盘/板块 beta 基准。指数在腾讯源用 sh/sz 前缀取。
INDICES = [
    ("上证指数", "sh000001"),
    ("深证成指", "sz399001"),
    ("创业板指", "sz399006"),
    ("沪深300", "sh000300"),
    ("科创50", "sh000688"),
]

# 卖出备注里形如 "盈亏-8733.24" / "盈亏 123.4" 的已实现盈亏
_PNL_RE = re.compile(r"盈亏\s*([+-]?\d+(?:\.\d+)?)")


def _read_state(p):
    fp = os.path.join(BASE, p)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_csv(p):
    fp = os.path.join(BASE, p)
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _fnum(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def _parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s.strip(), fmt)
        except Exception:
            continue
    return None


def week_bounds(week_str=None, now=None):
    """返回 (本周一0点, 本周结束=now或周日23:59:59, 上周一0点, 上周日23:59:59, iso标签)。"""
    now = now or dt.datetime.now()
    if week_str:
        # 形如 2026-W27
        y, w = week_str.upper().split("-W")
        monday = dt.datetime.strptime(f"{y}-W{int(w):02d}-1", "%G-W%V-%u")
    else:
        monday = (now - dt.timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
    this_start = monday
    this_end = min(now, monday + dt.timedelta(days=6, hours=23, minutes=59, seconds=59))
    prev_start = monday - dt.timedelta(days=7)
    prev_end = monday - dt.timedelta(seconds=1)
    iso = monday.strftime("%G-W%V")
    return this_start, this_end, prev_start, prev_end, iso


def _last_point_before(ecurve, deadline):
    """equity 曲线里 <=deadline 的最后一个点(总资产,收益率,时间)。无则 None。"""
    best = None
    for r in ecurve:
        t = _parse_ts(r.get("时间", ""))
        if t and t <= deadline:
            if not best or t > best[0]:
                best = (t, _fnum(r.get("总资产")), _fnum(r.get("累计收益率%")))
    return best


def _first_point_after(ecurve, start):
    """equity 曲线里 >=start 的第一个点(用于本周起点，取上周末若无则用本周首点)。"""
    best = None
    for r in ecurve:
        t = _parse_ts(r.get("时间", ""))
        if t and t >= start:
            if not best or t < best[0]:
                best = (t, _fnum(r.get("总资产")), _fnum(r.get("累计收益率%")))
    return best


def collect_indices(use_live=True):
    """抓指数周涨跌基准。返回 {name:{price,prev_close,pct}}。取不到置 null。"""
    out = {}
    if not use_live:
        return {n: None for n, _ in INDICES}
    try:
        import quote_sources as qs
    except Exception as e:
        return {n: None for n, _ in INDICES}
    for name, sym in INDICES:
        try:
            txt = qs._get(f"http://qt.gtimg.cn/q={sym}", gbk=True)
            f = txt.split('"')[1].split("~")
            price = float(f[3]); prev = float(f[4])
            pct = (price / prev - 1) * 100 if prev else None
            out[name] = {"price": round(price, 2), "prev_close": round(prev, 2),
                         "pct_vs_prevclose": round(pct, 3) if pct is not None else None}
        except Exception as e:
            out[name] = None
    return out


def _prev_week_rounds(prev_iso):
    """读上一周的数据底座，取各组 round，供 freshness_gate 的 non_advancing_round 用。

    这条闸门本来就写好了，但一直是死代码：它要求 state 里有 previous_round，
    而全代码库无一处写入。与其新开一个 sidecar 状态文件，不如直接用已经落盘的
    上周 weekly_data_*.json —— 那里面本来就记着每组的 round。
    文件不存在（首次复盘/历史缺失）返回空 dict，保持沉默不误报。
    """
    path = os.path.join(BASE, f"weekly_data_{prev_iso}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return {g.get("name"): g.get("round") for g in data.get("groups", [])
            if g.get("exists") and g.get("round") is not None}


def collect(week_str=None, use_live=True):
    now = dt.datetime.now()
    ts, te, ps, pe, iso = week_bounds(week_str, now)
    prev_rounds = _prev_week_rounds(ps.strftime("%G-W%V"))

    groups = []
    for name, desc, sp, ep, tp in ACCOUNTS:
        st = _read_state(sp)
        eq = _read_csv(ep)
        tr = _read_csv(tp)

        if st is None:
            groups.append({"name": name, "desc": desc, "exists": False})
            continue

        # ---- 数据完整性闸门（确定性体检，先于任何归因）----
        try:
            import integrity_gate
            gate = integrity_gate.check(tr, st, init_cash=st.get("init_cash", 1_000_000.0))
        except Exception as e:
            gate = {"clean": None, "red_flags": [
                {"check": "gate_error", "severity": "warn",
                 "detail": f"完整性闸门执行失败: {repr(e)[:120]}"}]}

        # ---- 数据新鲜度闸门：账本自洽不等于本周确实推进 ----
        try:
            import freshness_gate
            # 用副本注入 previous_round，绝不回写真实 state.json
            gate_state = dict(st)
            if name in prev_rounds:
                gate_state["previous_round"] = prev_rounds[name]
            freshness = freshness_gate.check(
                gate_state, eq, now=now, review_start=ts, review_end=te,
            )
        except Exception as e:
            freshness = {"fresh": None, "red_flags": [
                {"check": "freshness_gate_error", "severity": "error",
                 "detail": f"新鲜度闸门执行失败: {repr(e)[:120]}"}]}

        init = st.get("init_cash", 1_000_000.0)
        cash = st.get("cash", init)
        pos = st.get("positions", {})

        # ---- 周维度收益增量 ----
        # 本周末点：<=本周结束的最后一个权益点
        # 本周起点：上周末最后一个点(优先)，若无则本周第一个点
        end_pt = _last_point_before(eq, te)
        prev_pt = _last_point_before(eq, pe)   # 上周末
        start_pt = prev_pt or _first_point_after(eq, ts)

        week_ret = None
        week_pnl = None
        if end_pt and start_pt and start_pt[1]:
            week_pnl = round(end_pt[1] - start_pt[1], 2)
            week_ret = round((end_pt[1] / start_pt[1] - 1) * 100, 3)

        cum_ret = round(end_pt[2], 3) if end_pt else None
        total = round(end_pt[1], 2) if end_pt else round(cash, 2)

        # stale 账户保留原始观测值用于数据诊断，但不得进入收益排名/归因。
        if freshness.get("fresh") is False:
            week_ret = None
            week_pnl = None

        # ---- 本周成交明细(时间落在[ts,te]) ----
        week_trades = []
        realized_this_week = 0.0
        win = loss = 0
        for r in tr:
            t = _parse_ts(r.get("时间", ""))
            if not (t and ts <= t <= te):
                continue
            note = r.get("备注", "")
            m = _PNL_RE.search(note)
            pnl = round(_fnum(m.group(1)), 2) if m else None
            if pnl is not None:
                realized_this_week += pnl
                if pnl > 0:
                    win += 1
                elif pnl < 0:
                    loss += 1
            week_trades.append({
                "t": r.get("时间", ""), "side": r.get("方向", ""),
                "code": r.get("代码", ""), "name": r.get("名称", ""),
                "price": r.get("价格", ""), "qty": r.get("数量", ""),
                "amount": r.get("成交额", ""), "pnl": pnl, "note": note,
            })

        # ---- 期末持仓(供 agent 逐票研究) ----
        positions = []
        for code, p in pos.items():
            positions.append({
                "code": code, "name": p.get("name", ""),
                "qty": p.get("qty", 0), "available": p.get("available", 0),
                "cost": round(p.get("cost", 0), 3),
            })

        groups.append({
            "name": name, "desc": desc, "exists": True,
            "round": st.get("round", 0),
            "integrity": gate,
            "freshness": freshness,
            "stale": freshness.get("fresh") is False,
            "init": init, "cash": round(cash, 2), "total_asset": total,
            "cum_ret_pct": cum_ret,
            "week_ret_pct": week_ret, "week_pnl": week_pnl,
            "week_start_asset": round(start_pt[1], 2) if start_pt else None,
            "week_start_ts": start_pt[0].strftime("%Y-%m-%d %H:%M") if start_pt else None,
            "week_end_ts": end_pt[0].strftime("%Y-%m-%d %H:%M") if end_pt else None,
            "week_realized_pnl": round(realized_this_week, 2),
            "week_win": win, "week_loss": loss,
            "week_trade_count": len(week_trades),
            "positions": positions,
            "week_trades": week_trades,
        })

    # ---- 全组去重持仓 → 供 Layer2 情报采集的研究清单 ----
    holdings = {}
    for g in groups:
        if not g.get("exists"):
            continue
        for p in g["positions"]:
            h = holdings.setdefault(p["code"], {"code": p["code"], "name": p["name"], "held_by": []})
            h["held_by"].append(g["name"])
    research_universe = sorted(holdings.values(), key=lambda x: len(x["held_by"]), reverse=True)

    indices = collect_indices(use_live)

    # ---- 完整性总闸：任一组脏 → 全局告警，复盘归因必须先停 ----
    dirty = [g["name"] for g in groups
             if g.get("exists") and g.get("integrity", {}).get("clean") is False]
    stale = [g["name"] for g in groups
             if g.get("exists") and g.get("freshness", {}).get("fresh") is False]
    invalid = sorted(set(dirty + stale))
    integrity_summary = {
        "all_clean": len(dirty) == 0 and len(stale) == 0,
        "dirty_groups": dirty,
        "stale_groups": stale,
        "excluded_groups": invalid,
        "gate_note": ("所有账户通过完整性和新鲜度校验，净值可用于比较。" if not invalid else
                      f"⚠ 检测到 {len(invalid)} 个账户不可用于精确排名"
                      f"（账本污染: {len(dirty)}，数据过期: {len(stale)}）。"
                      "脏或 stale 账户仅做定性说明，不参与收益排名和归因。"),
    }

    return {
        "meta": {
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "review_week": iso,
            "this_week": [ts.strftime("%Y-%m-%d"), te.strftime("%Y-%m-%d %H:%M")],
            "prev_week": [ps.strftime("%Y-%m-%d"), pe.strftime("%Y-%m-%d")],
            "note": "本周收益增量=本周末权益点vs上周末权益点(周边界对齐自然周)；指数为生成时实时值，作beta基准。",
        },
        "integrity_summary": integrity_summary,
        "indices": indices,
        "groups": groups,
        "research_universe": research_universe,
    }


def main():
    use_live = "--no-live" not in sys.argv
    week_str = None
    if "--week" in sys.argv:
        week_str = sys.argv[sys.argv.index("--week") + 1]

    data = collect(week_str, use_live)
    iso = data["meta"]["review_week"]
    out = os.path.join(BASE, f"weekly_data_{iso}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 控制台速览
    print(f"Layer1 数据已生成: {out}")
    print(f"复盘周: {iso} | 本周: {data['meta']['this_week'][0]} ~ {data['meta']['this_week'][1]}")

    # 完整性闸门先行披露
    isum = data["integrity_summary"]
    print("\n== 数据完整性闸门 ==")
    if isum["all_clean"]:
        print("  ✅ 全部账户通过完整性和新鲜度校验，净值可用于比较。")
    else:
        print(f"  🔴 不可用于精确比较的账户: {', '.join(isum['excluded_groups'])}")
        for g in data["groups"]:
            if not g.get("exists"):
                continue
            for gate_name in ("integrity", "freshness"):
                gate = g.get(gate_name, {})
                if gate.get("clean") is False or gate.get("fresh") is False:
                    print(f"    · {g['name']} [{gate_name}]: {len(gate.get('red_flags', []))} 红旗")
                    for fl in gate.get("red_flags", []):
                        mark = "🔴" if fl["severity"] == "error" else "🟡"
                        print(f"      {mark} [{fl['check']}] {fl['detail']}")

    print("\n== 指数 beta 基准(生成时实时) ==")
    for n, v in data["indices"].items():
        if v:
            print(f"  {n}: {v['price']} (较昨收 {v['pct_vs_prevclose']:+}%)")
        else:
            print(f"  {n}: 未取到")
    print("\n== 各组本周表现 ==")
    for g in data["groups"]:
        if not g.get("exists"):
            print(f"  {g['name']}: 未初始化")
            continue
        wr = f"{g['week_ret_pct']:+.3f}%" if g["week_ret_pct"] is not None else "N/A"
        wp = f"{g['week_pnl']:+,.0f}" if g["week_pnl"] is not None else "N/A"
        print(f"  {g['name']}: 本周{wr} (盈亏{wp}) | 累计{g['cum_ret_pct']}% | "
              f"总资产{g['total_asset']:,.0f} | 本周成交{g['week_trade_count']}笔 "
              f"{g['week_win']}胜{g['week_loss']}负 | 持仓{len(g['positions'])}只")
    print(f"\n== 研究清单(去重持仓 {len(data['research_universe'])} 只，供情报采集) ==")
    for h in data["research_universe"]:
        print(f"  {h['code']} {h['name']}: 被 {len(h['held_by'])} 组持有")


if __name__ == "__main__":
    import market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    main()
