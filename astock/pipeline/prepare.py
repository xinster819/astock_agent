"""
B组(实验组) 决策包生成器 —— 三段式 prepare→[agent决策]→execute 的第一段。
跑这一步：跨日结算(T+1解冻) + 拉实时行情 + 算技术指标 + 规则候选信号，
打包成 groupB/decision_input.json，交给调度器的 agent 回合去做最终买卖判断。

设计要点：
  - 强制 ASTOCK_GROUP=B，账本与A组(纯规则对照)完全隔离。
  - 决策包里同时给出"规则候选"(rule_candidates)，让 agent 有锚点：可采纳、可否决、可调仓位，
    但不强制——agent 是决策者，规则只是参谋。
  - 只输出"事实+候选"，不替 agent 做决定；真正的 buy/sell 由 agent 写进 decision_output.json。
  - 现价/涨跌停等敏感字段全部来自实时多源交叉验证(market.get_quote)，脏价/分歧已被置0。
用法：ASTOCK_GROUP=B python3 prepare.py   （建议由调度器设好环境变量）
"""
import os
import json
import datetime as dt

os.environ.setdefault("ASTOCK_GROUP", "B")  # 兜底，确保隔离

from astock.data import market
from astock.core import broker
from astock.strategy import signals
from astock.runtime import clock as market_time

# 组名由 ASTOCK_GROUP 决定（B/C/D…），账本目录与 broker 完全一致，实现多 agent 组隔离。
GROUP = os.environ.get("ASTOCK_GROUP", "B").strip().upper()
GDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"group{GROUP}")
os.makedirs(GDIR, exist_ok=True)
INPUT_PATH = os.path.join(GDIR, "decision_input.json")


def build():
    market_time.enforce()   # 幂等：确保写账本前进程时区=交易所时区
    signals.clear_indicator_cache()   # 轮首清指标缓存，保证本轮取到最新日线
    now = dt.datetime.now()
    trading, status = market.is_trading_now(now)

    st = broker.load_state()
    settled = broker.settle_new_day(st)
    if settled:
        broker.save_state(st)

    pool = signals.load_pool()
    codes = list(set(pool) | set(st["positions"].keys()))
    quotes = market.get_quotes(codes)
    market.log_spread(quotes)

    # 行情质量分类（与 run.py 同口径），让 agent 知道哪些票本轮不可信
    bad, warn = [], []
    for c, q in quotes.items():
        if q.get("error") or q.get("dirty") or q.get("diverge") or q.get("price", 0) <= 0:
            bad.append(c)
        elif str(q.get("cross", "")).startswith("single_source"):
            warn.append(c)

    # 技术指标（复用规则层的指标计算）
    indicators = {}
    for c in codes:
        ind = signals._indicators(c)
        if ind:
            indicators[c] = {k: (round(v, 4) if isinstance(v, float) else v)
                             for k, v in ind.items()}

    # 规则候选信号（agent 的参谋，不是命令）
    rule_signals = signals.generate_signals(st, quotes)

    # 真实新闻注入（仅 C组多空辩论 / D组新闻情绪需要）——根治"输入无新闻→
    # agent 凭训练记忆编新闻"的幻觉根因。只对【持仓 + 规则候选】相关票取数
    # （≤10 只，控制请求数），每条带发布时间+来源+链接并标注时效，超期/无源
    # 由 agent 侧闸门拒作唯一买入依据。取数失败降级为 unavailable，不阻塞主流程。
    news = None
    if GROUP in ("C", "D"):
        try:
            from astock.data import news_feed
            news_codes = list(st["positions"].keys()) + [
                s["code"] for s in rule_signals if s.get("code")]
            news = news_feed.get_news_for_codes(news_codes)
        except Exception as e:
            news = {"_error": repr(e)[:120]}

    # 估算总资产与仓位约束，供 agent 控制风险
    mv, total = broker.market_value(st, quotes)

    # 持仓快照（含浮盈，方便 agent 判断止盈止损）
    positions = {}
    for code, p in st["positions"].items():
        q = quotes.get(code) or {}
        px = q.get("price") or p["cost"]
        positions[code] = {
            "name": p.get("name", ""), "qty": p["qty"],
            "available": p.get("available", 0), "cost": p["cost"],
            "price": px,
            "pnl_pct": round((px / p["cost"] - 1) * 100, 2) if p["cost"] else 0,
        }

    pack = {
        "group": GROUP,
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market_status": status,
        "is_trading": trading,
        "account": {
            "cash": st["cash"], "init_cash": st["init_cash"],
            "market_value": mv, "total_asset": total,
            "return_pct": round((total / st["init_cash"] - 1) * 100, 3),
            "round": st.get("round", 0),
        },
        "constraints": {
            "max_positions": signals.MAX_POSITIONS,
            "max_weight_per_stock": signals.MAX_WEIGHT,
            "max_new_per_round": signals.MAX_NEW_PER_ROUND,
            "budget_per_stock": round(total * signals.MAX_WEIGHT, 2),
            "note": "买卖最终都会过 broker 硬校验：拒涨停买/跌停卖、T+1冻结不可卖、"
                    "资金不足自动缩量、脏价(price=0)拒单、单票上限与每轮上限。"
                    "agent 只能在这些硬闸内决策。",
        },
        "positions": positions,
        "quotes": {c: {k: q.get(k) for k in
                       ("name", "price", "prev_close", "limit_up", "limit_down",
                        "open", "high", "low", "cross")}
                   for c, q in quotes.items()},
        "indicators": indicators,
        "quote_quality": {"bad": bad, "single_source": warn},
        "rule_candidates": rule_signals,
        "pool": pool,
    }

    # 新闻闸门：只有 C/D 组带 news 字段。附使用契约，压制"无源即编"的幻觉。
    if news is not None:
        pack["news"] = news
        pack["news_gate"] = (
            "新闻使用铁律：①只能援引本 news 字段内的条目，禁止凭记忆补充任何"
            "未在此列出的'新闻/传闻/研报'；②每条新闻带 published+source+url，"
            "stale=true(超T-3或无时间)者不得作为唯一买入依据，仅可作风险提示；"
            "③status=unavailable/empty 表示本轮无可信新闻，应退回纯技术面决策，"
            "不得假设'无消息=利好'。")

    with open(INPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=2)
    return pack


if __name__ == "__main__":
    from astock.runtime import clock as market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    import sys
    import time
    import random
    # 抖动：与A组错峰，避免两组在整点同时打行情源。
    # 上限300s(5分)：因 build 本身约100s，留足余量使 jitter+build 稳在单条Bash 600s上限内。
    if "--no-jitter" not in sys.argv:
        lo = int(os.environ.get("JITTER_MIN", "30"))
        hi = int(os.environ.get("JITTER_MAX", "300"))
        w = random.randint(lo, hi)
        print(f"[jitter] 随机延时 {w}s（{w//60}分{w%60}秒）后开始采集…")
        time.sleep(w)
    p = build()
    print(f"决策包已生成: {INPUT_PATH}")
    print(f"  市场: {p['market_status']} | 总资产: {p['account']['total_asset']:,.2f} "
          f"| 现金: {p['account']['cash']:,.2f} | 持仓数: {len(p['positions'])}")
    print(f"  行情异常: {len(p['quote_quality']['bad'])} 只 | "
          f"单源降级: {len(p['quote_quality']['single_source'])} 只")
    print(f"  规则候选信号: {len(p['rule_candidates'])} 条 -> {p['rule_candidates']}")
