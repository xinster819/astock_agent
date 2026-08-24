"""
实验组报告脚本：查看所有实验组的账户状态。
用法: python3 report_exp.py [exp_id]
"""
import sys
import os
from astock.core import experiments as exp_manager


def format_report(exp_id):
    """格式化单个实验组的报告"""
    config = exp_manager.get_exp_config(exp_id)
    st = exp_manager.load_exp_state(exp_id)

    if not config or not st:
        return f"实验组 {exp_id} 不存在或状态异常"

    # 计算当前市值（需要实时行情）
    from astock.data import market
    from astock.core import broker

    codes = list(set(st.get("positions", {}).keys()))
    quotes = market.get_quotes(codes) if codes else {}
    mv, total = broker.market_value(st, quotes)
    ret = (total / st["init_cash"] - 1) * 100

    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  [{exp_id}] {config.get('name', exp_id)}")
    lines.append(f"  {config.get('desc', '')}")
    lines.append(f"{'='*60}")
    lines.append(f"初始资金 : {st['init_cash']:,.2f}")
    lines.append(f"现金余额 : {st['cash']:,.2f}")
    lines.append(f"持仓市值 : {mv:,.2f}")
    lines.append(f"总  资产 : {total:,.2f}")
    lines.append(f"累计收益 : {ret:+.2f}% ({total - st['init_cash']:+.2f})")
    lines.append(f"已运行   : {st.get('round', 0)} 轮")
    lines.append(f"{'-'*60}")

    # 持仓详情
    positions = st.get("positions", {})
    if positions:
        lines.append("持仓详情:")
        for code, p in positions.items():
            q = quotes.get(code, {})
            px = q.get("price", p.get("cost", 0))
            pos_value = px * p["qty"]
            pnl = (px / p["cost"] - 1) * 100 if p["cost"] else 0
            lines.append(f"  {code} {p.get('name', '')}: {p['qty']}股 @成本{p['cost']:.2f} "
                        f"现价{px:.2f} 市值{pos_value:,.0f} 盈亏{pnl:+.1f}%")
    else:
        lines.append("当前空仓。")

    lines.append(f"{'-'*60}")

    # 最近交易
    trades_path = exp_manager.get_exp_trades_path(exp_id)
    if os.path.exists(trades_path):
        with open(trades_path, "r", encoding="utf-8") as f:
            lines_list = f.readlines()
            if len(lines_list) > 1:
                lines.append("最近交易:")
                for line in lines_list[-5:]:  # 最近5条
                    if line.strip() and not line.startswith("时间"):
                        parts = line.strip().split(",")
                        if len(parts) >= 10:
                            lines.append(f"  {parts[0]} {parts[1]} {parts[2]} {parts[3]} "
                                        f"{parts[4]}元 x{parts[5]}股 - {parts[9]}")

    return "\n".join(lines)


def summary_table():
    """生成所有实验组的汇总表"""
    from astock.data import market
    from astock.core import broker

    exps = exp_manager.list_experiments()
    if not exps:
        return "暂无实验组数据"

    lines = []
    lines.append(f"{'='*90}")
    lines.append(f"{'实验组':<8} {'名称':<12} {'轮次':<6} {'现金':<12} {'总资产':<12} {'收益率':<10} {'持仓数':<6}")
    lines.append(f"{'-'*90}")

    for exp in exps:
        st = exp_manager.load_exp_state(exp['id'])
        if not st:
            continue

        # 计算总资产
        codes = list(st.get("positions", {}).keys())
        quotes = market.get_quotes(codes) if codes else {}
        mv, total = broker.market_value(st, quotes)
        ret = (total / st["init_cash"] - 1) * 100
        pos_count = len(st.get("positions", {}))

        lines.append(f"{exp['id']:<8} {exp['name']:<12} {st.get('round', 0):<6} "
                    f"{st['cash']:<12,.0f} {total:<12,.0f} {ret:<+9.1f}% {pos_count:<6}")

    lines.append(f"{'='*90}")
    return "\n".join(lines)


if __name__ == "__main__":
    from astock.runtime import clock as market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    if len(sys.argv) > 1:
        exp_id = sys.argv[1]
        print(format_report(exp_id))
    else:
        print("实验组汇总报告")
        print()
        print(summary_table())
        print()
        print("查看单个实验组详情: python3 report_exp.py [exp_id]")
        print("例如: python3 report_exp.py exp1")
