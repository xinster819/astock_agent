"""B组 agent 决策落地器：确定性校验、组合风控与持久化执行。"""
import os
import sys
import json
import datetime as dt

os.environ.setdefault("ASTOCK_GROUP", "B")

from astock.data import market
from astock.core import broker
from astock.strategy import signals
from astock.guards import trade as trade_guard
from astock.guards import risk as risk_guard
from astock.guards import regime as market_regime_mod
from astock.runtime import clock as market_time

# 组名由 ASTOCK_GROUP 决定（B/C/D…），与 prepare.py / broker 一致，实现多 agent 组隔离。
GROUP = os.environ.get("ASTOCK_GROUP", "B").strip().upper()
GDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"group{GROUP}")
OUTPUT_PATH = os.path.join(GDIR, "decision_output.json")
DECISION_LOG = os.path.join(GDIR, "decision_log.csv")


def _dlog(row):
    new = not os.path.exists(DECISION_LOG)
    with open(DECISION_LOG, "a", encoding="utf-8") as f:
        if new:
            f.write("时间,动作,代码,数量,结果,信息,理由\n")
        f.write(",".join(str(x).replace(",", "；") for x in row) + "\n")


def _tag(force):
    """强制轮次在成交备注里留痕，便于事后把非交易时段的成交与正常轮次区分开。"""
    return "[强制/非交易时段]" if force else ""


def _valid_decision(d):
    if not isinstance(d, dict):
        return False, "非对象"
    action = str(d.get("action", "")).lower().strip()
    if action not in ("buy", "sell"):
        return False, f"非法动作:{d.get('action')}"
    code = str(d.get("code", "")).strip()
    if not (code.isdigit() and len(code) == 6):
        return False, f"非法代码:{d.get('code')}"
    try:
        qty = int(d.get("qty", 0))
    except Exception:
        return False, f"非法数量:{d.get('qty')}"
    if qty <= 0:
        return False, f"数量<=0:{qty}"
    return True, {"action": action, "code": code, "qty": qty,
                  "reason": str(d.get("reason", ""))[:50]}


def _load_risk_guard(st, equity, now):
    guard = risk_guard.RiskGuard(
        daily_loss_limit=0.02, max_drawdown=0.10,
        consecutive_loss_limit=3, loss_cooldown_trades=3,
        stop_loss_cooldown_trades=3,
    )
    guard.restore(st.get("risk_guard"))
    today = now.strftime("%Y-%m-%d")
    if st.get("risk_date") != today:
        st["risk_date"] = today
        st["day_start_equity"] = equity
    st["day_start_equity"] = float(st.get("day_start_equity", equity))
    st["peak_equity"] = max(float(st.get("peak_equity", equity)), equity)
    return guard


def _market_regime():
    """集中化 regime（market_regime 模块）。返回 RegimeResult。
    旧的 except→risk_off 兜底已下沉到 market_regime.classify，此处只透传结果。"""
    return market_regime_mod.classify()


INPUT_PATH = os.path.join(GDIR, "decision_input.json")


def decision_freshness(out_path=None, in_path=None, raw=None):
    """校验 agent 决策文件是否属于【本轮】。返回 (ok, reason)。

    为什么必须有这道校验：execute.py 原先拿到 decision_output.json 就直接执行，
    对它是什么时候写的毫无判断。2026-08-23 实测：groupB/C/D 里躺着 08-20 写的
    决策文件（因当时时区停摆，execute 从未消费、也就从未归档），一旦进入交易日
    就会把三天前的决策按今天的价格下单——与 07-31 时区事故同属"静默失效"家族。

    判据（任一不过即拒用，且必须大声说出来，不静默跳过）：
      1) 必须存在本轮 decision_input.json —— 没有决策包就没有决策依据；
      2) decision_output 的 mtime 必须晚于 decision_input —— 否则是上一轮的残留；
      3) 若 output 里带了 input_ts 字段，必须与本轮 decision_input 的 ts 一致
         （可选字段，带了就校验，是比 mtime 更强的溯源）。
    """
    out_path = out_path or OUTPUT_PATH
    in_path = in_path or INPUT_PATH
    if not os.path.exists(in_path):
        return False, f"缺少本轮决策包 {os.path.basename(in_path)}，无法确认决策依据"
    try:
        out_m = os.path.getmtime(out_path)
        in_m = os.path.getmtime(in_path)
    except OSError as exc:
        return False, f"读取文件时间失败：{exc}"
    if out_m <= in_m:
        age_h = (in_m - out_m) / 3600
        return False, (f"决策文件早于本轮决策包 {age_h:.1f}h，判为上一轮残留，拒绝执行"
                       f"（避免用旧决策按新价格下单）")
    if isinstance(raw, dict) and raw.get("input_ts"):
        try:
            with open(in_path, encoding="utf-8") as f:
                cur_ts = json.load(f).get("ts")
        except Exception:
            cur_ts = None
        if cur_ts and str(raw["input_ts"]).strip() != str(cur_ts).strip():
            return False, (f"决策文件声明的 input_ts={raw['input_ts']} "
                           f"与本轮决策包 ts={cur_ts} 不符，拒绝执行")
    return True, ""


def execute(verbose=True, force=False):
    """落地 agent 决策。

    force=True：跳过【交易时段】判断，用最近一次可得行情强制成交
    （与 run.py / run_exp.py 的 --force 语义一致，供测试与手动补轮次用）。
    ⚠ 只放行时段判断，其余硬校验（涨跌停、T+1、整手、资金、单票权重、持仓数、
    组合风控、决策文件新鲜度）一律照旧。强制轮次的成交备注会带
    [强制/非交易时段] 标记，让账本自己说明这笔不是正常时段产生的。
    """
    log = []
    market_time.enforce()   # 幂等：确保写账本前进程时区=交易所时区
    def out(message):
        log.append(message)
        if verbose:
            print(message)
    try:
        with trade_guard.account_lock(GROUP):
            return _execute_locked(out, log, force=force)
    except trade_guard.LockBusy as exc:
        out(f"⏭ {GROUP}组跳过本轮：{exc}")
        return "\n".join(log)


def _execute_locked(out, log, force=False):
    now = dt.datetime.now()
    trading, status = market.is_trading_now(now)
    out(f"=== {GROUP}组 execute {now.strftime('%Y-%m-%d %H:%M:%S')} | 市场: {status} ===")
    if force and not trading:
        out(f"⚠ --force：非交易时段({status})仍强制落地，按最近可得行情成交；"
            f"成交备注将带 [强制/非交易时段] 标记。")
    st = broker.load_state()
    if broker.settle_new_day(st):
        out("跨日结算：T+1 冻结份额已解冻。")

    if not trading and not force:
        codes = list(st.get("positions", {}).keys())
        quotes = market.get_quotes(codes) if codes else {}
        broker.snapshot_equity(st, quotes)
        broker.save_state(st)
        out("非交易时段，跳过下单并刷新权益。")
        return "\n".join(log)

    ok, why = trade_guard.can_execute(st, now=now.timestamp())
    if not ok:
        codes = list(st.get("positions", {}).keys())
        quotes = market.get_quotes(codes) if codes else {}
        broker.snapshot_equity(st, quotes)
        broker.save_state(st)
        out(f"⏭ {GROUP}组跳过下单（防抖）：{why}")
        return "\n".join(log)

    decisions = []
    if not os.path.exists(OUTPUT_PATH):
        out(f"⚠ 未找到 agent 决策文件 {OUTPUT_PATH}，本轮不下单（仅更新权益）。")
    else:
        try:
            with open(OUTPUT_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            fresh, why = decision_freshness(raw=raw)
            if not fresh:
                out(f"🔴 {GROUP}组 决策文件未通过新鲜度校验，本轮不下单：{why}")
                raw = {}
            decisions = raw.get("decisions", []) if isinstance(raw, dict) else []
            if isinstance(raw, dict) and raw.get("comment"):
                out(f"agent 思路: {raw['comment'][:200]}")
        except Exception as exc:
            out(f"⚠ 决策文件解析失败({repr(exc)[:60]})，本轮不下单。")

    codes = list({d.get("code") for d in decisions
                  if isinstance(d, dict) and d.get("code")}
                 | set(st.get("positions", {}).keys()))
    quotes = market.get_quotes(codes) if codes else {}
    _, equity = broker.market_value(st, quotes)
    guard = _load_risk_guard(st, equity, now)
    regime_res = _market_regime()
    regime = regime_res.regime
    if regime_res.degraded:
        out(f"  ⚠ 市场状态为降级值[{regime_res.source}]：{regime_res.detail}")
    else:
        out(f"  · 市场状态 {regime}（{regime_res.detail}）")
    max_new = 0 if regime == "risk_off" else signals.MAX_NEW_PER_ROUND
    if regime == "high_volatility":
        max_new = min(max_new, 1)

    normalized = []
    for decision in decisions:
        valid, result = _valid_decision(decision)
        if valid:
            normalized.append(result)
        else:
            out(f"  ✗ 跳过非法决策: {result} | 原始={decision}")

    new_buys = 0
    for decision in sorted(normalized,
                           key=lambda x: 0 if x["action"] == "sell" else 1):
        quote = quotes.get(decision["code"])
        if not quote:
            out(f"  ✗ {decision['code']} 无行情，跳过")
            continue
        if decision["action"] == "buy":
            if new_buys >= max_new:
                out(f"  ✗ {decision['code']} 当前市场状态拒绝新开仓")
                continue
            # 组合硬限制：单票权重与持仓数量不能由 agent 决策绕过。
            current_positions = st.get("positions", {})
            if decision["code"] not in current_positions:
                if len([p for p in current_positions.values() if p.get("qty", 0) > 0]) >= signals.MAX_POSITIONS:
                    out(f"  ✗ {decision['code']} 持仓已达上限 {signals.MAX_POSITIONS}，拒绝买入")
                    continue
            price = float(quote.get("price", 0) or 0)
            if price <= 0:
                out(f"  ✗ {decision['code']} 无有效现价，跳过")
                continue
            current = current_positions.get(decision["code"], {})
            current_value = float(current.get("qty", 0)) * price
            max_value = float(equity) * signals.MAX_WEIGHT
            room = max_value - current_value
            max_qty = int(max(0.0, room) / price // 100 * 100)
            if max_qty <= 0:
                out(f"  ✗ {decision['code']} 已达到单票权重上限，拒绝买入")
                continue
            qty = min(decision["qty"], max_qty)
            risk = guard.allow(equity, st["day_start_equity"],
                               st["peak_equity"], decision["code"])
            if not risk.allowed:
                out(f"  ✗ {decision['code']} 风控拒绝买入：{risk.reason}")
                continue
            ok2, message = broker.buy(st, quote, qty,
                                       _tag(force) + f"agent{GROUP}:" + decision["reason"])
            if ok2:
                new_buys += 1
        else:
            position = st.get("positions", {}).get(decision["code"], {})
            before = ((quote.get("price", 0) / position.get("cost", 0) - 1)
                      if position.get("cost") else 0)
            available_before = int(position.get("available", 0))
            actual_qty = min(int(decision["qty"]), available_before)
            if actual_qty < int(position.get("qty", 0)) and actual_qty % 100 != 0:
                actual_qty = actual_qty // 100 * 100
            ok2, message = broker.sell(st, quote, decision["qty"],
                                       _tag(force) + f"agent{GROUP}:" + decision["reason"])
            if ok2:
                guard.record_trade(
                    before * position.get("cost", 0) * actual_qty,
                    decision["code"],
                    stop_loss="止损" in decision.get("reason", ""),
                )
        out(("  ✓ " if ok2 else "  ✗ ") + message)
        _dlog([now.strftime("%H:%M:%S"), decision["action"], decision["code"],
               decision["qty"], "成交" if ok2 else "拒", message[:60],
               decision["reason"]])

    st["risk_guard"] = guard.to_dict()
    st["market_regime"] = regime
    st["market_regime_source"] = regime_res.source
    st["market_regime_degraded"] = regime_res.degraded
    # 只有真正走完下单分支才会到这里。记录当天日期，供 freshness_gate 的
    # stalled_engine 检查识别"进程在跑但从未进入下单分支"的停摆（2026-07-31 事故）。
    st["round"] = st.get("round", 0) + 1
    st["last_trading_round_date"] = now.strftime("%Y-%m-%d")
    total, ret = broker.snapshot_equity(st, quotes)
    st["peak_equity"] = max(float(st.get("peak_equity", total)), total)
    broker.save_state(st)
    out(f"{GROUP}组本轮结束 #{st['round']}。总资产 {total:,.2f}，累计收益 {ret}%，现金 {st['cash']:,.2f}")

    if os.path.exists(OUTPUT_PATH):
        archive = os.path.join(GDIR,
                               f"decision_output_{now.strftime('%Y%m%d_%H%M%S')}.json")
        os.rename(OUTPUT_PATH, archive)
        out(f"已归档本轮决策 -> {os.path.basename(archive)}")
    return "\n".join(log)


if __name__ == "__main__":
    from astock.runtime import clock as market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    execute(force="--force" in sys.argv)
