"""
A股规则引擎 + 模拟账户（虚拟盘）。
严格实现的真实A股规则：
  - T+1：当日买入的股票当日不可卖出（可用数量与持仓数量分离）。
  - 涨跌停：现价>=涨停价不可买（封板买不进），现价<=跌停价不可卖。
  - 整手：买入必须100股整数倍；卖出可不足100（清仓允许）。
  - 费用：佣金双边万2.5(最低5元)、印花税卖出单边千1、过户费双边万0.1。
  - 资金：买入需 现金>=金额+费用；卖出回笼现金=金额-费用。
  - 涨跌停板上的撮合：本引擎按“能不能挂上/成交”近似——封死则拒单。
状态持久化到 state.json，每一轮读-改-写，保证“一直跑”可断点续跑。
"""
import json
import os
import datetime as dt

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
TRADES_PATH = os.path.join(os.path.dirname(__file__), "trades.csv")
EQUITY_PATH = os.path.join(os.path.dirname(__file__), "equity.csv")

# ---- 实验分组隔离 ----
# 环境变量 ASTOCK_GROUP 指定账本分组，实现 A/B 组完全隔离、互不污染：
#   未设置 或 "A" -> 对照组(纯规则)，沿用根目录 state/trades/equity（保持历史不变）。
#   "B" 等       -> 实验组(agent决策)，落到子目录 group<X>/，独立账本。
# 两组复用同一套买卖硬校验(buy/sell)与费率，唯一区别是文件路径与初始资金各自独立。
_GROUP = os.environ.get("ASTOCK_GROUP", "A").strip().upper()
if _GROUP and _GROUP != "A":
    _GDIR = os.path.join(os.path.dirname(__file__), f"group{_GROUP}")
    os.makedirs(_GDIR, exist_ok=True)
    STATE_PATH = os.path.join(_GDIR, "state.json")
    TRADES_PATH = os.path.join(_GDIR, "trades.csv")
    EQUITY_PATH = os.path.join(_GDIR, "equity.csv")

# ---- 费率配置（可调）----
COMMISSION_RATE = 0.00025   # 佣金 万2.5
COMMISSION_MIN = 5.0        # 单笔最低5元
STAMP_TAX_RATE = 0.001      # 印花税 千1，仅卖出
TRANSFER_RATE = 0.00001     # 过户费 万0.1，双边
INIT_CASH = 1_000_000.0     # 初始资金100万


def _today():
    return dt.datetime.now().strftime("%Y-%m-%d")


def buy_fee(amount):
    comm = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    transfer = amount * TRANSFER_RATE
    return round(comm + transfer, 2)


def sell_fee(amount):
    comm = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp = amount * STAMP_TAX_RATE
    transfer = amount * TRANSFER_RATE
    return round(comm + stamp + transfer, 2)


def load_state():
    if not os.path.exists(STATE_PATH):
        st = {
            "cash": INIT_CASH,
            "init_cash": INIT_CASH,
            "positions": {},   # code -> {qty,available,cost,name}
            "created": _today(),
            "last_settle_date": _today(),
            "round": 0,
        }
        save_state(st)
        return st
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def settle_new_day(st):
    """跨日结算：把当日买入冻结的份额解冻为可用（T+1 -> T日次日可卖）。"""
    today = _today()
    if st.get("last_settle_date") != today:
        for code, p in st["positions"].items():
            p["available"] = p["qty"]
        st["last_settle_date"] = today
        return True
    return False


def _log_trade(row):
    new = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", encoding="utf-8") as f:
        if new:
            f.write("时间,方向,代码,名称,价格,数量,成交额,费用,现金余额,备注\n")
        f.write(",".join(str(x) for x in row) + "\n")


def buy(st, quote, qty, reason=""):
    """按现价市价买入 qty 股。返回 (ok, msg)。"""
    code, name, price = quote["code"], quote.get("name", ""), quote.get("price", 0)
    if price <= 0:
        return False, f"{code} 无有效现价（休市/停牌），拒买"
    if quote.get("limit_up") and price >= quote["limit_up"]:
        return False, f"{code} 已涨停 {price}，买不进（封板），拒买"
    if qty % 100 != 0:
        qty = (qty // 100) * 100
    if qty <= 0:
        return False, f"{code} 数量不足一手，拒买"
    amount = round(price * qty, 2)
    fee = buy_fee(amount)
    if st["cash"] < amount + fee:
        # 资金不足则按可用资金缩减到整百
        max_qty = int(st["cash"] / (price * (1 + COMMISSION_RATE + TRANSFER_RATE)) // 100 * 100)
        if max_qty <= 0:
            return False, f"{code} 现金不足，拒买"
        qty = max_qty
        amount = round(price * qty, 2)
        fee = buy_fee(amount)
    st["cash"] = round(st["cash"] - amount - fee, 2)
    p = st["positions"].get(code, {"qty": 0, "available": 0, "cost": 0.0, "name": name})
    new_qty = p["qty"] + qty
    p["cost"] = round((p["cost"] * p["qty"] + amount + fee) / new_qty, 4)  # 含费成本
    p["qty"] = new_qty
    # T+1：当日买入不增加 available
    p["name"] = name or p.get("name", "")
    st["positions"][code] = p
    _log_trade([quote.get("ts", _today()), "买入", code, name, price, qty,
                amount, fee, st["cash"], reason])
    return True, f"买入 {name}({code}) {qty}股 @{price}，成交额{amount}，费用{fee}"


def sell(st, quote, qty, reason=""):
    """按现价市价卖出 qty 股（受 T+1 可用数量约束）。返回 (ok,msg)。"""
    code, name, price = quote["code"], quote.get("name", ""), quote.get("price", 0)
    p = st["positions"].get(code)
    if not p or p["qty"] <= 0:
        return False, f"{code} 无持仓，拒卖"
    if price <= 0:
        return False, f"{code} 无有效现价（休市/停牌），拒卖"
    if quote.get("limit_down") and price <= quote["limit_down"]:
        return False, f"{code} 已跌停 {price}，卖不出（封板），拒卖"
    avail = p.get("available", 0)
    if avail <= 0:
        return False, f"{code} 无可用份额（T+1冻结），拒卖"
    qty = min(qty, avail)
    # 卖出允许不足整百（清仓），但若非清仓则取整百
    if qty < p["qty"] and qty % 100 != 0:
        qty = (qty // 100) * 100
    if qty <= 0:
        return False, f"{code} 可卖数量不足，拒卖"
    amount = round(price * qty, 2)
    fee = sell_fee(amount)
    st["cash"] = round(st["cash"] + amount - fee, 2)
    cost_part = p["cost"] * qty
    realized = round(amount - fee - cost_part, 2)
    p["qty"] -= qty
    p["available"] -= qty
    if p["qty"] <= 0:
        st["positions"].pop(code, None)
    else:
        st["positions"][code] = p
    _log_trade([quote.get("ts", _today()), "卖出", code, name, price, qty,
                amount, fee, st["cash"], f"{reason} 盈亏{realized}"])
    return True, f"卖出 {name}({code}) {qty}股 @{price}，回笼{amount}，费用{fee}，已实现盈亏{realized}"


def market_value(st, quotes):
    """按最新行情计算持仓市值与总资产。"""
    mv = 0.0
    for code, p in st["positions"].items():
        q = quotes.get(code) or {}
        px = q.get("price") or p["cost"]
        mv += px * p["qty"]
    return round(mv, 2), round(st["cash"] + mv, 2)


def snapshot_equity(st, quotes):
    mv, total = market_value(st, quotes)
    ret = round((total / st["init_cash"] - 1) * 100, 3)
    new = not os.path.exists(EQUITY_PATH)
    with open(EQUITY_PATH, "a", encoding="utf-8") as f:
        if new:
            f.write("时间,现金,持仓市值,总资产,累计收益率%\n")
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts},{st['cash']},{mv},{total},{ret}\n")
    return total, ret
