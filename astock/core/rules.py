"""rules · A 股撮合规则（**纯函数，零 IO**）。

【为什么把规则和账本读写拆开】
重构前 `broker.buy()` 一边算撮合、一边往 trades.csv 追加行、一边依赖模块级
全局路径。三件事绑死带来的代价是：想测一条撮合规则，必须先准备文件系统、
设好环境变量、再 reload 模块。结果就是——**这个仓库里唯一动钱的模块，
测试覆盖率是 0**。

拆开后，本模块只做一件事：给定 (账户状态, 行情, 数量)，算出「这笔能不能成、
成了之后账户变成什么样」，返回一个 `Execution` 值对象。落盘由 `ledger` 负责，
两者在 `account` 层组合。规则从此可以纯内存测试。

【实现的真实 A 股规则】
  T+1     当日买入不计入可用数量，次日结算才解冻
  涨跌停  现价 >= 涨停价不可买（封板买不进）；<= 跌停价不可卖
  整手    买入必须 100 股整数倍；卖出允许不足整手（清仓）
  费用    见 `fees`，买入费计入持仓成本（含费成本价）
  资金    买入需 现金 >= 成交额 + 费用，不足则按可用资金逐手缩减
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astock.core import fees
from astock.runtime import clock

LOT = 100


@dataclass(frozen=True)
class Fill:
    """一笔成交的完整事实。写账本、算盈亏、事后重放对账都只认它。"""

    side: str            # "买入" / "卖出"
    code: str
    name: str
    price: float
    qty: int
    amount: float
    fee: float
    cash_after: float
    reason: str
    realized_pnl: float | None = None   # 仅卖出有值


@dataclass(frozen=True)
class Execution:
    """一次下单尝试的结果。ok=False 时 fill 必为 None，message 说明拒单原因。"""

    ok: bool
    message: str
    fill: Fill | None = None


def _reject(msg: str) -> Execution:
    return Execution(False, msg)


def _tradable_price(quote: dict[str, Any]) -> float:
    """行情里的可成交现价。0 表示不可成交（休市/停牌/多源分歧被拒）。"""
    try:
        return float(quote.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def buy(state: dict[str, Any], quote: dict[str, Any], qty: int, reason: str = "") -> Execution:
    """按现价买入 qty 股。成功时**就地修改** state 并返回带 Fill 的 Execution。

    现金不足时不是直接拒单，而是缩减到买得起的最大整手——这是模拟盘的刻意选择：
    真实下单同样是"能买多少买多少"，一律拒单会让满仓附近的策略行为失真。
    """
    code = quote["code"]
    name = quote.get("name", "") or ""
    price = _tradable_price(quote)

    if price <= 0:
        return _reject(f"{code} 无有效现价（休市/停牌/多源分歧），拒买")
    limit_up = quote.get("limit_up")
    if limit_up and price >= float(limit_up):
        return _reject(f"{code} 已涨停 {price}，买不进（封板），拒买")

    qty = (int(qty) // LOT) * LOT
    if qty <= 0:
        return _reject(f"{code} 数量不足一手，拒买")

    cash = float(state["cash"])
    qty = _affordable_qty(cash, price, qty)
    if qty <= 0:
        return _reject(f"{code} 现金不足，拒买")

    amount = round(price * qty, 2)
    fee = fees.buy_fee(amount)
    cash_after = round(cash - amount - fee, 2)

    pos = state["positions"].get(code) or {"qty": 0, "available": 0, "cost": 0.0, "name": name}
    new_qty = pos["qty"] + qty
    # 含费成本价：买入费摊进成本，卖出时的已实现盈亏才是真实到手数
    pos["cost"] = round((pos["cost"] * pos["qty"] + amount + fee) / new_qty, 4)
    pos["qty"] = new_qty
    pos["name"] = name or pos.get("name", "")
    # T+1：当日买入不增加 available，由 settle_new_day 次日解冻
    pos.setdefault("available", 0)
    # 建仓时刻：时间止损（strategy 的 time_stop_days）唯一的判据。
    # ⚠ 重构前只有 run_exp 里那份复制的 _buy_exp 会写这个字段，broker.buy 不写，
    # 于是 A/B/C/D 组的时间止损**永远不会触发**——配置项在，行为不在。
    # 现在只有一处买入逻辑，字段自然对所有账户一致。
    pos.setdefault("opened_at", clock.naive_now().isoformat(timespec="seconds"))

    state["cash"] = cash_after
    state["positions"][code] = pos

    fill = Fill(side="买入", code=code, name=name, price=price, qty=qty,
                amount=amount, fee=fee, cash_after=cash_after, reason=reason)
    return Execution(True, f"买入 {name}({code}) {qty}股 @{price}，成交额{amount}，费用{fee}", fill)


def _affordable_qty(cash: float, price: float, wanted: int) -> int:
    """算出现金真正买得起的整手数量。

    ⚠ 这里必须**逐手回退验证**，不能只用 `fees.max_affordable_qty` 的解析解。
    佣金有 5 元保底，是个阶梯函数：解析解按比例费率反推，小额单会低估费用，
    刚好卡在边界时会让现金穿负（integrity_gate 的"负现金"红旗即由此触发）。
    实例：现金 994、价 9.9 —— 解析解给出 100 股，实需 995.01，穿负 1.01 元。
    """
    # 先用解析解跳到附近（避免大额单上循环几千次），再逐手回退到真买得起为止。
    # 保底费只有 5 元，回退通常 0~1 手就收敛。
    qty = min(wanted, fees.max_affordable_qty(cash, price, LOT))
    while qty > 0:
        amount = round(price * qty, 2)
        if cash >= amount + fees.buy_fee(amount):
            return qty
        qty -= LOT
    return 0


def sell(state: dict[str, Any], quote: dict[str, Any], qty: int, reason: str = "") -> Execution:
    """按现价卖出 qty 股，受 T+1 可用数量约束。成功时就地修改 state。"""
    code = quote["code"]
    name = quote.get("name", "") or ""
    price = _tradable_price(quote)

    pos = state["positions"].get(code)
    if not pos or pos["qty"] <= 0:
        return _reject(f"{code} 无持仓，拒卖")
    if price <= 0:
        return _reject(f"{code} 无有效现价（休市/停牌/多源分歧），拒卖")
    limit_down = quote.get("limit_down")
    if limit_down and price <= float(limit_down):
        return _reject(f"{code} 已跌停 {price}，卖不出（封板），拒卖")

    available = int(pos.get("available", 0))
    if available <= 0:
        return _reject(f"{code} 无可用份额（T+1 冻结），拒卖")

    qty = min(int(qty), available)
    # 清仓允许不足整手（零股只能一次性卖出）；非清仓则必须整手
    if qty < pos["qty"] and qty % LOT != 0:
        qty = (qty // LOT) * LOT
    if qty <= 0:
        return _reject(f"{code} 可卖数量不足，拒卖")

    amount = round(price * qty, 2)
    fee = fees.sell_fee(amount)
    cash_after = round(float(state["cash"]) + amount - fee, 2)
    realized = round(amount - fee - pos["cost"] * qty, 2)

    pos["qty"] -= qty
    pos["available"] = available - qty
    state["cash"] = cash_after
    if pos["qty"] <= 0:
        state["positions"].pop(code, None)
    else:
        state["positions"][code] = pos

    fill = Fill(side="卖出", code=code, name=name, price=price, qty=qty,
                amount=amount, fee=fee, cash_after=cash_after,
                reason=reason, realized_pnl=realized)
    return Execution(
        True,
        f"卖出 {name}({code}) {qty}股 @{price}，回笼{amount}，费用{fee}，已实现盈亏{realized}",
        fill,
    )


def settle_new_day(state: dict[str, Any], today: str) -> bool:
    """跨日结算：把 T+1 冻结份额解冻为可用。已结算过则返回 False（幂等）。"""
    if state.get("last_settle_date") == today:
        return False
    for pos in state["positions"].values():
        pos["available"] = pos["qty"]
    state["last_settle_date"] = today
    return True


def market_value(state: dict[str, Any], quotes: dict[str, Any]) -> tuple[float, float]:
    """按最新行情算 (持仓市值, 总资产)。

    取不到价的票按成本估值——这是保守选择：绝不用陈旧价格制造浮盈浮亏。
    这类估值降级由上层的行情质量体检显式报告，不在这里静默处理。
    """
    mv = 0.0
    for code, pos in state["positions"].items():
        quote = quotes.get(code) or {}
        px = _tradable_price(quote) or pos["cost"]
        mv += px * pos["qty"]
    return round(mv, 2), round(float(state["cash"]) + mv, 2)


def total_return_pct(state: dict[str, Any], total: float) -> float:
    """相对初始资金的累计收益率（%）。"""
    init = float(state.get("init_cash") or 0)
    if init <= 0:
        return 0.0
    return round((total / init - 1) * 100, 3)
