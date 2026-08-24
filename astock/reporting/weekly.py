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
import datetime as dt
import json
import os

from astock.reporting import metrics, roster
from astock.runtime import files, paths

# 大盘/板块 beta 基准。指数在腾讯源用 sh/sz 前缀取。
INDICES = [
    ("上证指数", "sh000001"),
    ("深证成指", "sz399001"),
    ("创业板指", "sz399006"),
    ("沪深300", "sh000300"),
    ("科创50", "sh000688"),
]


def collect_indices(use_live=True):
    """抓指数周涨跌基准。返回 {name:{price,prev_close,pct}}。取不到置 null。"""
    out = {}
    if not use_live:
        return {n: None for n, _ in INDICES}
    try:
        from astock.data import quote_sources as qs
    except Exception:
        return {n: None for n, _ in INDICES}
    for name, sym in INDICES:
        try:
            txt = qs._get(f"http://qt.gtimg.cn/q={sym}", gbk=True)
            f = txt.split('"')[1].split("~")
            price = float(f[3])
            prev = float(f[4])
            pct = (price / prev - 1) * 100 if prev else None
            out[name] = {"price": round(price, 2), "prev_close": round(prev, 2),
                         "pct_vs_prevclose": round(pct, 3) if pct is not None else None}
        except Exception:
            out[name] = None
    return out


def _prev_week_rounds(prev_iso):
    """读上一周的数据底座，取各组 round，供 freshness_gate 的 non_advancing_round 用。

    这条闸门本来就写好了，但一直是死代码：它要求 state 里有 previous_round，
    而全代码库无一处写入。与其新开一个 sidecar 状态文件，不如直接用已经落盘的
    上周 weekly_data_*.json —— 那里面本来就记着每组的 round。
    文件不存在（首次复盘/历史缺失）返回空 dict，保持沉默不误报。
    """
    path = str(paths.weekly_data(prev_iso))
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    # 优先按 account id 匹配。历史文件只有 name，退回按 name 匹配——
    # 而 name 现在以配置为准，配置改名会让按名匹配落空（实测 exp9 就改过名）。
    # 落空只会让 non_advancing_round 这一条闸门沉默，不会误报，可以安全过渡。
    rounds = {}
    for g in data.get("groups", []):
        if not (g.get("exists") and g.get("round") is not None):
            continue
        for key in (g.get("account"), g.get("name")):
            if key:
                rounds[key] = g["round"]
    return rounds


def collect(week_str=None, use_live=True):
    """采集一周的复盘数据底座。返回可直接落 JSON 的 dict。

    这份 JSON 是对照实验的**结论出口**：谁跑赢谁、赢在哪、哪些组的数据不可用于
    排名，全部由它决定。所以每一步算术都走 `metrics` 里的纯函数（可单测），
    账户名册走 `roster`（以配置为权威），本函数只负责把它们串起来。
    """
    now = dt.datetime.now()
    window = metrics.week_bounds(week_str, now)
    prev_rounds = _prev_week_rounds(window.prev_start.strftime("%G-W%V"))

    groups = []
    for account in roster.roster():
        st = files.read_json(account.paths.state)
        eq = files.read_csv_rows(account.paths.equity)
        tr = files.read_csv_rows(account.paths.trades)

        if st is None:
            groups.append({"account": account.account, "name": account.label,
                           "desc": account.desc, "exists": False})
            continue

        # ---- 数据完整性闸门（确定性体检，先于任何归因）----
        try:
            from astock.guards import integrity as integrity_gate
            gate = integrity_gate.check(tr, st, init_cash=st.get("init_cash", 1_000_000.0))
        except Exception as e:
            gate = {"clean": None, "red_flags": [
                {"check": "gate_error", "severity": "warn",
                 "detail": f"完整性闸门执行失败: {repr(e)[:120]}"}]}

        # ---- 数据新鲜度闸门：账本自洽不等于本周确实推进 ----
        try:
            from astock.guards import freshness as freshness_gate
            # 用副本注入 previous_round，绝不回写真实 state.json
            gate_state = dict(st)
            previous = prev_rounds.get(account.account, prev_rounds.get(account.label))
            if previous is not None:
                gate_state["previous_round"] = previous
            fresh = freshness_gate.check(
                gate_state, eq, now=now,
                review_start=window.start, review_end=window.end,
            )
        except Exception as e:
            fresh = {"fresh": None, "red_flags": [
                {"check": "freshness_gate_error", "severity": "error",
                 "detail": f"新鲜度闸门执行失败: {repr(e)[:120]}"}]}

        init = st.get("init_cash", 1_000_000.0)
        cash = st.get("cash", init)

        # ---- 周维度收益增量 ----
        end_pt = metrics.last_point_before(eq, window.end)
        start_pt = (metrics.last_point_before(eq, window.prev_end)
                    or metrics.first_point_after(eq, window.start))
        week = metrics.week_return(eq, window)

        cum_ret = round(end_pt.cumulative_return_pct, 3) if end_pt else None
        total = round(end_pt.total, 2) if end_pt else round(cash, 2)

        # stale 账户保留原始观测值用于数据诊断，但不得进入收益排名/归因。
        if fresh.get("fresh") is False:
            week = metrics.WeekReturn(None, None)

        stats = metrics.trades_in_window(tr, window)

        positions = [
            {"code": code, "name": p.get("name", ""), "qty": p.get("qty", 0),
             "available": p.get("available", 0), "cost": round(p.get("cost", 0), 3)}
            for code, p in st.get("positions", {}).items()
        ]

        groups.append({
            "account": account.account,
            "name": account.label, "desc": account.desc, "exists": True,
            "round": st.get("round", 0),
            "integrity": gate,
            "freshness": fresh,
            "stale": fresh.get("fresh") is False,
            "init": init, "cash": round(cash, 2), "total_asset": total,
            "cum_ret_pct": cum_ret,
            "week_ret_pct": week.pct, "week_pnl": week.pnl,
            "week_start_asset": round(start_pt.total, 2) if start_pt else None,
            "week_start_ts": start_pt.at.strftime("%Y-%m-%d %H:%M") if start_pt else None,
            "week_end_ts": end_pt.at.strftime("%Y-%m-%d %H:%M") if end_pt else None,
            "week_realized_pnl": stats.realized,
            "week_win": stats.wins, "week_loss": stats.losses,
            "week_win_rate": stats.win_rate,
            "week_trade_count": len(stats.trades),
            "positions": positions,
            "week_trades": stats.trades,
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
            "review_week": window.iso,
            "this_week": [window.start.strftime("%Y-%m-%d"),
                          window.end.strftime("%Y-%m-%d %H:%M")],
            "prev_week": [window.prev_start.strftime("%Y-%m-%d"),
                          window.prev_end.strftime("%Y-%m-%d")],
            "note": "本周收益增量=本周末权益点vs上周末权益点(周边界对齐自然周)；指数为生成时实时值，作beta基准。",
        },
        "integrity_summary": integrity_summary,
        "indices": indices,
        "groups": groups,
        "research_universe": research_universe,
    }


def main(week_str=None, use_live=True, printer=print):
    """采集并落盘一周的数据底座，返回输出路径。

    参数由调用方（CLI）给出，不再自己翻 sys.argv——参数解析集中在 cli/main.py，
    这里才能被测试直接调用。
    """
    data = collect(week_str, use_live)
    iso = data["meta"]["review_week"]
    out = paths.reports_dir() / f"weekly_data_{iso}.json"
    # 原子写：这份 JSON 是下游周报正文的唯一输入，半截文件会让复盘读到残缺数据
    files.write_json_atomic(out, data)
    print = printer

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

    return out
