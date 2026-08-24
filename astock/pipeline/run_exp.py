"""
实验组运行脚本：支持多组并行策略对比。
用法: python3 run_exp.py [exp_id] [--force]
示例: python3 run_exp.py exp1
       python3 run_exp.py exp2 --force
"""
import sys
import random
import time
import datetime as dt

from astock.data import market
from astock.core import broker
from astock.strategy import signals
from astock.core import experiments as exp_manager
from astock.guards import trade as trade_guard
from astock.guards import risk as risk_guard
from astock.runtime import clock as market_time


def run_experiment(exp_id, force=False, verbose=True):
    """运行指定实验组的一轮交易"""
    log = []
    market_time.enforce()   # 幂等：确保写账本前进程时区=交易所时区
    def out(m):
        log.append(m)
        if verbose:
            print(m)

    # 加载实验组配置
    config = exp_manager.get_exp_config(exp_id)
    if not config:
        out(f"错误: 实验组 {exp_id} 不存在")
        return None

    # ---- 执行层互斥：整段"读state→下单→写state"必须串行化，杜绝并发幽灵成交 ----
    try:
        with trade_guard.account_lock(exp_id):
            return _run_experiment_locked(exp_id, config, force, out, log)
    except trade_guard.LockBusy as e:
        out(f"⏭ 跳过本轮：{e}")
        return "\n".join(log)


def _current_market_regime(config=None):
    """从沪深300历史收盘价计算市场状态（集中化 market_regime 模块）。

    旧实现两个坑已修复：①指数取数走 get_index_hist 多源兜底而非误用个股接口；
    ②数据源失效不再一律 risk_off——优先回退最近一次真实观测，仅冷启动才用保守默认，
    且降级值会被显式标记。冷启动默认仍可被实验组配置的 market_regime_fallback 覆盖。
    """
    from astock.guards import regime as _mr
    fallback = (config or {}).get("market_regime_fallback", "risk_off")
    res = _mr.classify(cold_start_default=fallback)
    if res.degraded:
        print(f"  ⚠ 市场状态降级[{res.source}]：{res.detail}")
    return res.regime


def _refresh_equity_exp(exp_id, st, quotes, *, write=True):
    """统一刷新权益、峰值并可选写入权益快照。"""
    from astock.core.broker import market_value
    mv, total = market_value(st, quotes)
    st["peak_equity"] = max(float(st.get("peak_equity", total)), total)
    if write:
        _log_equity_exp(exp_id, st, mv, total)
    return mv, total


def _run_experiment_locked(exp_id, config, force, out, log):
    """持有账户互斥锁后执行的核心逻辑（状态在锁内加载，冷却判定必见最新时间戳）。"""
    # 状态在锁内加载，确保能读到上一执行者已落盘的 last_run_ts
    st = exp_manager.load_exp_state(exp_id)
    if not st:
        out(f"错误: 无法加载实验组 {exp_id} 的状态")
        return None

    signals.clear_indicator_cache()   # 轮首清指标缓存，保证本轮取到最新日线
    now = dt.datetime.now()
    trading, status = market.is_trading_now(now)
    out(f"=== [{exp_id}] {config.get('name', exp_id)} | {now.strftime('%Y-%m-%d %H:%M:%S')} | 市场: {status} ===")

    # 跨日结算
    from astock.core.broker import settle_new_day
    if settle_new_day(st):
        out("跨日结算：T+1 冻结份额已解冻为可用。")

    if not trading and not force:
        # 非交易时段
        pool = signals.load_pool()
        quotes = market.get_quotes(list(set(pool) | set(st["positions"].keys())))
        mv, total = _refresh_equity_exp(exp_id, st, quotes)
        exp_manager.save_exp_state(exp_id, st)
        out(f"非交易时段，跳过下单。当前总资产 {total:,.2f}，累计收益 {(total/st['init_cash']-1)*100:.1f}%")
        return "\n".join(log)

    # ---- 冷却去抖：距上次成功执行不足冷却期 → 判为重复触发，跳过下单 ----
    ok, why = trade_guard.can_execute(st, now=now.timestamp())
    if not ok:
        # 不下单，但仍刷新估值与状态（last_run_ts 未变，防重触发时间戳不被延后）
        pool = signals.load_pool()
        quotes = market.get_quotes(list(set(pool) | set(st["positions"].keys())))
        mv, total = _refresh_equity_exp(exp_id, st, quotes)
        exp_manager.save_exp_state(exp_id, st)
        out(f"⏭ 跳过下单（防抖）：{why}")
        return "\n".join(log)

    # 交易时段
    codes = list(set(signals.load_pool()) | set(st["positions"].keys()))
    quotes = market.get_quotes(codes)

    from astock.core.broker import market_value
    _, equity = market_value(st, quotes)
    risk = _load_risk_guard(st, config, equity, now)

    # 记录价差
    market.log_spread(quotes)

    # 价差采样
    try:
        n, _ = market.sample_spreads()
        if n:
            out(f"价差采样：沪深300采集 {n} 只有效双源样本")
    except Exception as e:
        out(f"价差采样跳过：{repr(e)[:80]}")

    # 行情质量检查
    bad, warn = [], []
    for c, q in quotes.items():
        if q.get("error") or q.get("dirty") or q.get("diverge") or q.get("price", 0) <= 0:
            bad.append(c)
        elif str(q.get("cross", "")).startswith("single_source"):
            warn.append(c)
    if bad:
        details = []
        for c in bad:
            q = quotes[c]
            why = q.get("diverge") or q.get("dirty") or q.get("error") or "现价0"
            details.append(f"{c}({why})")
        out(f"⚠ 行情异常 {len(bad)}/{len(quotes)} 只：" + "; ".join(details[:5]))

    # 生成信号（传入实验组配置与动态市场状态）
    signal_config = dict(config)
    signal_config["market_regime"] = _current_market_regime(config)
    orders = signals.generate_signals(st, quotes, exp_config=signal_config)
    out(f"生成信号 {len(orders)} 条")

    # 执行交易（需要修改broker的日志路径）；先卖后买。
    for s in sorted(orders, key=lambda x: 0 if x["action"] == "sell" else 1):
        q = quotes.get(s["code"])
        if not q:
            continue
        if s["action"] == "buy":
            decision = risk.allow(equity, st["day_start_equity"], st["peak_equity"], s["code"])
            if not decision.allowed:
                out(f"  ✗ {s['code']} 风控拒绝买入：{decision.reason}")
                continue
            ok, msg = _buy_exp(st, q, s["qty"], s["reason"], exp_id)
        else:
            p = st.get("positions", {}).get(s["code"], {})
            before = (q.get("price", 0) / p.get("cost", 0) - 1) if p.get("cost") else 0
            ok, msg = _sell_exp(st, q, s["qty"], s["reason"], exp_id)
            if ok:
                risk.record_trade(before * p.get("cost", 0) * s["qty"], s["code"],
                                  stop_loss="止损" in s.get("reason", ""))
        out(("  ✓ " if ok else "  ✗ ") + msg)

    st["risk_guard"] = risk.to_dict()

    # 更新轮次和保存状态
    # 只有真正走完下单分支才会到这里。记录当天日期，供 freshness_gate 的
    # stalled_engine 检查识别"进程在跑但从未进入下单分支"的停摆（2026-07-31 事故）。
    st["round"] = st.get("round", 0) + 1
    st["last_trading_round_date"] = now.strftime("%Y-%m-%d")
    mv, total = _refresh_equity_exp(exp_id, st, quotes)

    exp_manager.save_exp_state(exp_id, st)
    ret = (total / st["init_cash"] - 1) * 100
    out(f"本轮结束 #{st['round']}。总资产 {total:,.2f}，累计收益 {ret:.1f}%，现金 {st['cash']:,.2f}")
    return "\n".join(log)


def _load_risk_guard(st, config, equity, now):
    """Load persisted portfolio guard and refresh daily/peak equity references."""
    rg = config.get("risk_guard", {}) if isinstance(config, dict) else {}
    guard = risk_guard.RiskGuard(
        daily_loss_limit=rg.get("daily_loss_limit", 0.02),
        max_drawdown=rg.get("max_drawdown", 0.10),
        consecutive_loss_limit=rg.get("consecutive_loss_limit", 3),
        loss_cooldown_trades=rg.get("loss_cooldown_trades", 3),
        stop_loss_cooldown_trades=rg.get("stop_loss_cooldown_trades", 3),
    )
    guard.restore(st.get("risk_guard"))
    today = now.strftime("%Y-%m-%d")
    if st.get("risk_date") != today:
        st["risk_date"] = today
        st["day_start_equity"] = equity
    st["day_start_equity"] = float(st.get("day_start_equity", equity))
    st["peak_equity"] = max(float(st.get("peak_equity", equity)), equity)
    return guard


def _buy_exp(st, quote, qty, reason, exp_id):
    """实验组买入（带独立日志）"""
    code, name, price = quote["code"], quote.get("name", ""), quote.get("price", 0)
    if price <= 0:
        return False, f"{code} 无有效现价，拒买"
    if quote.get("limit_up") and price >= quote["limit_up"]:
        return False, f"{code} 已涨停，拒买"
    if qty % 100 != 0:
        qty = (qty // 100) * 100
    if qty <= 0:
        return False, f"{code} 数量不足一手，拒买"

    amount = round(price * qty, 2)
    fee = _calc_buy_fee(amount)
    if st["cash"] < amount + fee:
        max_qty = int(st["cash"] / (price * 1.0026) // 100 * 100)  # 粗略估算含费
        if max_qty <= 0:
            return False, f"{code} 现金不足，拒买"
        qty = max_qty
        amount = round(price * qty, 2)
        fee = _calc_buy_fee(amount)

    st["cash"] = round(st["cash"] - amount - fee, 2)
    p = st["positions"].get(code, {"qty": 0, "available": 0, "cost": 0.0, "name": name})
    new_qty = p["qty"] + qty
    p["cost"] = round((p["cost"] * p["qty"] + amount + fee) / new_qty, 4)
    p["qty"] = new_qty
    p["opened_at"] = p.get("opened_at") or dt.datetime.now().isoformat(timespec="seconds")
    p["name"] = name or p.get("name", "")
    st["positions"][code] = p

    _log_trade_exp(exp_id, [dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "买入", code, name, price, qty,
                            amount, fee, st["cash"], reason])
    return True, f"买入 {name}({code}) {qty}股 @{price}，费用{fee}"


def _sell_exp(st, quote, qty, reason, exp_id):
    """实验组卖出（带独立日志）"""
    code, name, price = quote["code"], quote.get("name", ""), quote.get("price", 0)
    p = st["positions"].get(code)
    if not p or p["qty"] <= 0:
        return False, f"{code} 无持仓，拒卖"
    if price <= 0:
        return False, f"{code} 无有效现价，拒卖"
    if quote.get("limit_down") and price <= quote["limit_down"]:
        return False, f"{code} 已跌停，拒卖"

    avail = p.get("available", 0)
    if avail <= 0:
        return False, f"{code} 无可用份额（T+1），拒卖"

    qty = min(qty, avail)
    if qty < p["qty"] and qty % 100 != 0:
        qty = (qty // 100) * 100
    if qty <= 0:
        return False, f"{code} 可卖数量不足，拒卖"

    amount = round(price * qty, 2)
    fee = _calc_sell_fee(amount)
    st["cash"] = round(st["cash"] + amount - fee, 2)
    cost_part = p["cost"] * qty
    realized = round(amount - fee - cost_part, 2)
    p["qty"] -= qty
    p["available"] -= qty
    if p["qty"] <= 0:
        st["positions"].pop(code, None)
    else:
        st["positions"][code] = p

    _log_trade_exp(exp_id, [dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "卖出", code, name, price, qty,
                            amount, fee, st["cash"], f"{reason} 盈亏{realized}"])
    return True, f"卖出 {name}({code}) {qty}股 @{price}，盈亏{realized}"


def _calc_buy_fee(amount):
    comm = max(amount * 0.00025, 5.0)
    transfer = amount * 0.00001
    return round(comm + transfer, 2)


def _calc_sell_fee(amount):
    comm = max(amount * 0.00025, 5.0)
    stamp = amount * 0.001
    transfer = amount * 0.00001
    return round(comm + stamp + transfer, 2)


def _log_trade_exp(exp_id, row):
    """记录实验组交易"""
    import os
    path = exp_manager.get_exp_trades_path(exp_id)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("时间,方向,代码,名称,价格,数量,成交额,费用,现金余额,备注\n")
        f.write(",".join(str(x) for x in row) + "\n")


def _log_equity_exp(exp_id, st, mv, total):
    """记录实验组权益"""
    import os
    path = exp_manager.get_exp_equity_path(exp_id)
    new = not os.path.exists(path)
    ret = round((total / st["init_cash"] - 1) * 100, 3)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("时间,现金,持仓市值,总资产,累计收益率%\n")
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts},{st['cash']},{mv},{total},{ret}\n")


if __name__ == "__main__":
    from astock.runtime import clock as market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    import os

    # 解析参数
    exp_id = None
    force = "--force" in sys.argv

    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            exp_id = arg
            break

    # 如果没有指定实验组，列出所有
    if not exp_id:
        print("用法: python3 run_exp.py [exp_id] [--force]")
        print("\n可用实验组:")
        for exp in exp_manager.list_experiments():
            print(f"  {exp['id']}: {exp['name']} - {exp['desc']}")
            print(f"         轮次:{exp['round']} 现金:{exp['cash']:,.0f} 总资产:{exp['total']:,.0f}")
        sys.exit(0)

    # 随机抖动（错开整点）
    if "--no-jitter" not in sys.argv and not force:
        jitter = random.randint(60, 540)
        print(f"[jitter] 随机延时 {jitter}s 后开跑...")
        time.sleep(jitter)

    # 运行实验组
    run_experiment(exp_id, force=force)
