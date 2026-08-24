"""
B组(实验组) 决策包生成器 —— 三段式 prepare→[agent决策]→execute 的第一段。
跑这一步：跨日结算(T+1解冻) + 拉实时行情 + 算技术指标 + 规则候选信号，
打包成 groupB/decision_input.json，交给调度器的 agent 回合去做最终买卖判断。

设计要点：
  - 组名由参数或 $ASTOCK_GROUP 决定，账本与 A 组(纯规则对照)完全隔离。
  - 决策包里同时给出"规则候选"(rule_candidates)，让 agent 有锚点：可采纳、可否决、可调仓位，
    但不强制——agent 是决策者，规则只是参谋。
  - 只输出"事实+候选"，不替 agent 做决定；真正的 buy/sell 由 agent 写进 decision_output.json。
  - 现价/涨跌停等敏感字段全部来自实时多源交叉验证(market.get_quote)，脏价/分歧已被置0。
用法：astock prepare B      （或 ASTOCK_GROUP=B python -m astock.cli.main prepare）
"""
from __future__ import annotations

import datetime as dt
import os

from astock.core.account import Account
from astock.data import market
from astock.runtime import clock
from astock.runtime.paths import AccountPaths
from astock.strategy import signals


def default_group() -> str:
    """当前 agent 组。与 execute / 调度脚本共用同一个约定。"""
    return os.environ.get("ASTOCK_GROUP", "B").strip().upper() or "B"


def build(group: str | None = None) -> dict:
    """生成一个 agent 组的决策包，写入 group<X>/decision_input.json 并返回。"""
    group = (group or default_group()).upper()
    paths = AccountPaths.for_group(group).ensure_dirs()

    clock.enforce()                   # 幂等：确保写账本前进程时区=交易所时区
    signals.clear_indicator_cache()   # 轮首清指标缓存，保证本轮取到最新日线
    now = dt.datetime.now()
    trading, status = market.is_trading_now(now)

    account = Account.open(group)
    st = account.state
    if account.settle_new_day():
        account.save()

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
    if group in ("C", "D"):
        try:
            from astock.data import news_feed
            news_codes = list(st["positions"].keys()) + [
                s["code"] for s in rule_signals if s.get("code")]
            news = news_feed.get_news_for_codes(news_codes)
        except Exception as e:
            news = {"_error": repr(e)[:120]}

    # 估算总资产与仓位约束，供 agent 控制风险
    mv, total = account.market_value(quotes)

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
        "group": group,
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
            "note": "买卖最终都会过撮合规则硬校验：拒涨停买/跌停卖、T+1冻结不可卖、"
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

    # 原子写：决策包是 agent 回合的唯一输入，写到一半被中断会让 agent
    # 读到半截 JSON —— 那会表现为"agent 莫名其妙不下单"，又一种静默失效。
    from astock.core.ledger import write_json_atomic
    write_json_atomic(paths.decision_input, pack)
    return pack
