"""
integrity_gate · 账本数据完整性闸门（确定性、零主观、零外部依赖）
=====================================================================
把"发现执行 bug"从"复盘 agent 的自觉"降维成"脚本的必检"。

设计原则：
  - 只做能被账本内部证伪的**不变量校验**，不碰行情、不碰策略好坏判断。
  - 任一 error 级红旗 → 该账户本周净值不可信，复盘归因必须先停下。
  - 纯 stdlib，可被 weekly_collect.py 直接 import，也可独立跑。

检查项（check 字段）：
  cash_direction  买入现金必减/卖出必增，违反=幽灵成交铁证       [error]
  duplicate_order 同(代码,方向)在 DUP_WINDOW_SEC 秒内重复出现     [error]
  state_mismatch  重放 trades 的净持仓/现金 ≠ state.json          [error]
  negative_cash   state 现金为负                                  [error]
  position_overflow 持仓只数超过 MAX_POSITIONS（软约束）          [warn]
  out_of_pool     持仓标的不在股票池内（软约束）                  [warn]
"""
import datetime as dt

from astock.runtime import paths

DUP_WINDOW_SEC = 120     # 同票同向重复下单的时间窗
QTY_EPS = 0             # 持仓数量必须精确一致（整数股）
CASH_EPS = 1.0          # 现金对账容差（元）——费用四舍五入累积误差
MAX_POSITIONS = 5


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _i(x, d=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return d


def _parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime((s or "").strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def check(trades, state, init_cash=1_000_000.0, pool=None):
    """返回 {"clean": bool, "red_flags": [ {check,severity,detail,...} ]}"""
    flags = []
    trades = trades or []
    state = state or {}

    # ---- 1. 现金方向单调性：买入必减、卖出必增 ----
    # 逐笔比对"现金余额"列的前后差，与方向应当自洽。
    prev_cash = None
    for i, t in enumerate(trades):
        side = (t.get("方向") or "").strip()
        cash_after = _f(t.get("现金余额"))
        if prev_cash is not None:
            delta = cash_after - prev_cash
            if side == "买入" and delta > CASH_EPS:
                flags.append({
                    "check": "cash_direction", "severity": "error",
                    "detail": f"第{i+1}笔 买入 {t.get('名称','')}({t.get('代码','')}) 后现金却上涨 "
                              f"{prev_cash:.2f}→{cash_after:.2f}（幽灵成交/状态回滚）",
                    "row": i,
                })
            elif side == "卖出" and delta < -CASH_EPS:
                flags.append({
                    "check": "cash_direction", "severity": "error",
                    "detail": f"第{i+1}笔 卖出 {t.get('名称','')}({t.get('代码','')}) 后现金却下降 "
                              f"{prev_cash:.2f}→{cash_after:.2f}",
                    "row": i,
                })
        prev_cash = cash_after

    # ---- 2. 重复下单：同(代码,方向) 在极短窗口内重复 ----
    last_seen = {}   # (code, side) -> datetime
    for i, t in enumerate(trades):
        code = (t.get("代码") or "").strip()
        side = (t.get("方向") or "").strip()
        ts = _parse_ts(t.get("时间"))
        key = (code, side)
        if ts and key in last_seen:
            # 取绝对值：两笔同票同向成交只要相距在窗口内就可疑，与行序无关。
            # 旧实现只判 0 <= gap，历史账本里 12 行时间倒序，负 gap 会被静默跳过
            # ——判重闸门因此可能漏掉它本该抓的那一半幽灵成交。
            # 实测：改用绝对值后，13 个真实账本零新增红旗，是纯粹的加固。
            gap = abs((ts - last_seen[key]).total_seconds())
            if gap <= DUP_WINDOW_SEC:
                flags.append({
                    "check": "duplicate_order", "severity": "error",
                    "detail": f"{t.get('名称','')}({code}) {side} 在 {gap:.0f}s 内重复出现"
                              f"（第{i+1}笔，窗口≤{DUP_WINDOW_SEC}s）",
                    "row": i, "gap_sec": gap,
                })
        if ts:
            last_seen[key] = ts

    # ---- 3. 账实对账：重放 trades → 净持仓/现金，对比 state ----
    replay_qty = {}       # code -> 净股数
    cash = init_cash
    for t in trades:
        code = (t.get("代码") or "").strip()
        side = (t.get("方向") or "").strip()
        qty = _i(t.get("数量"))
        amount = _f(t.get("成交额"))
        fee = _f(t.get("费用"))
        if side == "买入":
            replay_qty[code] = replay_qty.get(code, 0) + qty
            cash -= (amount + fee)
        elif side == "卖出":
            replay_qty[code] = replay_qty.get(code, 0) - qty
            cash += (amount - fee)
    replay_qty = {c: q for c, q in replay_qty.items() if q != 0}

    positions = state.get("positions", {}) or {}
    state_qty = {c: _i(p.get("qty")) for c, p in positions.items() if _i(p.get("qty")) != 0}

    all_codes = set(replay_qty) | set(state_qty)
    for code in sorted(all_codes):
        rq = replay_qty.get(code, 0)
        sq = state_qty.get(code, 0)
        if abs(rq - sq) > QTY_EPS:
            flags.append({
                "check": "state_mismatch", "severity": "error",
                "detail": f"{code} 账实不符：trades 重放净持仓 {rq} 股，state 实际 {sq} 股"
                          f"（差 {rq - sq:+d}，幽灵成交或漏记）",
                "code": code, "replay_qty": rq, "state_qty": sq,
            })

    # 现金对账（仅当账户里有 state.cash 才比）
    if "cash" in state:
        state_cash = _f(state.get("cash"))
        if abs(cash - state_cash) > CASH_EPS:
            flags.append({
                "check": "state_mismatch", "severity": "error",
                "detail": f"现金账实不符：trades 重放 {cash:.2f}，state 实际 {state_cash:.2f}"
                          f"（差 {cash - state_cash:+.2f}）",
                "replay_cash": round(cash, 2), "state_cash": round(state_cash, 2),
            })

    # ---- 4. 负现金 ----
    if "cash" in state and _f(state.get("cash")) < -CASH_EPS:
        flags.append({
            "check": "negative_cash", "severity": "error",
            "detail": f"state 现金为负：{_f(state.get('cash')):.2f}",
        })

    # ---- 5. 持仓只数超限（软约束）----
    npos = len(state_qty)
    if npos > MAX_POSITIONS:
        flags.append({
            "check": "position_overflow", "severity": "warn",
            "detail": f"持仓 {npos} 只，超过上限 {MAX_POSITIONS}",
        })

    # ---- 6. 持仓不在池内（软约束，需传 pool）----
    if pool:
        poolset = set(pool)
        for code in state_qty:
            if code not in poolset:
                flags.append({
                    "check": "out_of_pool", "severity": "warn",
                    "detail": f"{code} 不在股票池内",
                    "code": code,
                })

    clean = not any(f["severity"] == "error" for f in flags)
    return {"clean": clean, "red_flags": flags}


# ---- CLI：对全部账户跑一遍体检 ----
def run_cli(printer=print) -> int:
    """对 13 个账户逐个体检，返回脏账户数。

    账户名单来自 `paths.all_accounts()`。此前这里硬编码了第 7 份 13 行账户表——
    同一份名单在仓库里前后出现过 7 次，改布局要改 7 处，漏一处就有账户体检不到。
    """
    from astock.runtime import files

    printer("== 账本完整性体检 ==")
    dirty = 0
    for account in paths.all_accounts():
        state = files.read_json(account.state)
        if state is None:
            printer(f"  {account.account}: 未初始化，跳过")
            continue
        trades = files.read_csv_rows(account.trades)
        result = check(trades, state, init_cash=state.get("init_cash", 1_000_000.0))
        if not result["clean"]:
            dirty += 1
        tag = "✅ clean" if result["clean"] else "🔴 DIRTY"
        printer(f"\n  {account.account}: {tag}  ({len(result['red_flags'])} 红旗)")
        for flag in result["red_flags"]:
            mark = "🔴" if flag["severity"] == "error" else "🟡"
            printer(f"    {mark} [{flag['check']}] {flag['detail']}")
    return dirty


#: 兼容旧调用名
_run_cli = run_cli
