"""
观察报告：随时运行查看 agent 炒股进展。
用法：
  python3 report.py          完整报告（账户+持仓+收益+最近交易）
  python3 report.py trades   只看买卖明细
  python3 report.py equity   只看收益曲线（文本）
"""
import sys
import os
import datetime as dt

from astock.data import market
from astock.core import broker


def fmt_money(x):
    return f"{x:,.2f}"


def full_report():
    st = broker.load_state()
    codes = list(st["positions"].keys())
    quotes = market.get_quotes(codes) if codes else {}
    mv, total = broker.market_value(st, quotes)
    ret = (total / st["init_cash"] - 1) * 100

    print("=" * 64)
    print(f"  A股虚拟交易 Agent | 报告时间 {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  市场状态: {market.is_trading_now()[1]} | 已运行轮次: {st.get('round',0)}")
    print("=" * 64)
    print(f"初始资金 : {fmt_money(st['init_cash'])}")
    print(f"现金余额 : {fmt_money(st['cash'])}")
    print(f"持仓市值 : {fmt_money(mv)}")
    print(f"总  资产 : {fmt_money(total)}")
    print(f"累计收益 : {ret:+.3f}%  ({fmt_money(total-st['init_cash'])})")
    print("-" * 64)

    if st["positions"]:
        print("当前持仓：")
        print(f"{'代码':<8}{'名称':<10}{'数量':>8}{'可用':>8}{'成本':>10}{'现价':>10}{'浮盈%':>9}")
        for code, p in st["positions"].items():
            q = quotes.get(code) or {}
            px = q.get("price") or p["cost"]
            pnl = (px / p["cost"] - 1) * 100 if p["cost"] else 0
            name = (p.get("name") or q.get("name") or "")[:5]
            print(f"{code:<8}{name:<10}{p['qty']:>8}{p.get('available',0):>8}"
                  f"{p['cost']:>10.3f}{px:>10.3f}{pnl:>+9.2f}")
    else:
        print("当前空仓。")
    print("-" * 64)
    show_trades(8)


def show_trades(limit=None):
    if not os.path.exists(broker.TRADES_PATH):
        print("暂无成交记录。")
        return
    with open(broker.TRADES_PATH, "r", encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    header, rows = lines[0], lines[1:]
    print(f"成交明细（共 {len(rows)} 笔" + (f"，显示最近 {limit} 笔" if limit else "") + "）：")
    print(header)
    for r in (rows[-limit:] if limit else rows):
        print(r)


def show_equity():
    if not os.path.exists(broker.EQUITY_PATH):
        print("暂无权益记录。")
        return
    with open(broker.EQUITY_PATH, "r", encoding="utf-8") as f:
        print(f.read().strip())


if __name__ == "__main__":
    from astock.runtime import clock as market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    arg = sys.argv[1] if len(sys.argv) > 1 else "full"
    if arg == "trades":
        show_trades()
    elif arg == "equity":
        show_equity()
    else:
        full_report()
