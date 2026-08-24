"""report · 账户观察报告（只读，绝不写账本）。

【合并说明】
重构前有两份报告实现：`report.py` 只认 A 组（靠 broker 的模块级全局路径），
`report_exp.py` 只认 exp 组（靠 exp_manager 的另一套路径函数），
B/C/D 组则**两份都覆盖不到**——想看 agent 组的持仓只能手动 cat JSON。

路径统一成 `AccountPaths` 之后，这个区分就没有存在的理由了：
一份实现，13 个账户都能看。
"""
from __future__ import annotations

from astock.core import experiments
from astock.core.account import Account
from astock.core.rules import total_return_pct
from astock.data import market
from astock.runtime import clock, paths

SEPARATOR = "=" * 68


def _quotes_for(account: Account) -> dict:
    codes = list(account.state.get("positions", {}))
    return market.get_quotes(codes) if codes else {}


def account_report(account_id: str, *, trade_limit: int = 8) -> str:
    """单个账户的完整报告：账户概览 + 持仓明细 + 最近成交。"""
    account = Account.open(account_id)
    st = account.state
    config = experiments.get_exp_config(account_id) or {}
    quotes = _quotes_for(account)
    mv, total = account.market_value(quotes)
    ret = total_return_pct(st, total)

    title = config.get("name") or f"{account_id} 组"
    lines = [
        SEPARATOR,
        f"  [{account_id}] {title}",
    ]
    if config.get("desc"):
        lines.append(f"  {config['desc']}")
    lines += [
        f"  报告时间 {clock.stamp()} | 市场: {market.is_trading_now()[1]}"
        f" | 已运行 {st.get('round', 0)} 轮",
        SEPARATOR,
        f"初始资金 : {st['init_cash']:>16,.2f}",
        f"现金余额 : {st['cash']:>16,.2f}",
        f"持仓市值 : {mv:>16,.2f}",
        f"总  资产 : {total:>16,.2f}",
        f"累计收益 : {ret:>+15.3f}%   ({total - st['init_cash']:+,.2f})",
        "-" * 68,
    ]
    lines += _position_lines(st, quotes)
    lines.append("-" * 68)
    lines += _trade_lines(account, trade_limit)
    return "\n".join(lines)


def _position_lines(st: dict, quotes: dict) -> list[str]:
    positions = st.get("positions", {})
    if not positions:
        return ["当前空仓。"]
    lines = ["当前持仓：",
             f"{'代码':<8}{'名称':<12}{'数量':>8}{'可用':>8}"
             f"{'成本':>10}{'现价':>10}{'浮盈%':>9}"]
    for code, pos in positions.items():
        quote = quotes.get(code) or {}
        # 取不到价就按成本估值——绝不用陈旧价格制造浮盈浮亏
        price = quote.get("price") or pos["cost"]
        pnl = (price / pos["cost"] - 1) * 100 if pos["cost"] else 0.0
        name = (pos.get("name") or quote.get("name") or "")[:6]
        lines.append(f"{code:<8}{name:<12}{pos['qty']:>8}{pos.get('available', 0):>8}"
                     f"{pos['cost']:>10.3f}{price:>10.3f}{pnl:>+9.2f}")
    return lines


def _trade_lines(account: Account, limit: int | None) -> list[str]:
    trades = account.ledger.read_trades()
    if not trades:
        return ["暂无成交记录。"]
    shown = trades[-limit:] if limit else trades
    header = f"成交明细（共 {len(trades)} 笔"
    header += f"，显示最近 {len(shown)} 笔）：" if limit else "）："
    lines = [header]
    for row in shown:
        lines.append(f"  {row.get('时间', '')} {row.get('方向', '')} "
                     f"{row.get('代码', '')} {row.get('名称', '')} "
                     f"@{row.get('价格', '')} x{row.get('数量', '')}股 "
                     f"- {row.get('备注', '')}")
    return lines


def summary_table(account_ids: list[str] | None = None) -> str:
    """全部账户的横向对比表。**对照实验的主视图**——13 个账户一屏看完。"""
    ids = account_ids or [a.account for a in paths.all_accounts()]
    lines = [
        "=" * 92,
        f"{'账户':<8}{'名称':<16}{'轮次':>6}{'现金':>14}{'总资产':>14}"
        f"{'收益率':>10}{'持仓数':>7}",
        "-" * 92,
    ]
    for account_id in ids:
        account = Account.open(account_id)
        st = account.state
        quotes = _quotes_for(account)
        _, total = account.market_value(quotes)
        config = experiments.get_exp_config(account_id) or {}
        name = (config.get("name") or f"{account_id}组")[:14]
        held = len([p for p in st.get("positions", {}).values() if p.get("qty", 0) > 0])
        lines.append(f"{account_id:<8}{name:<16}{st.get('round', 0):>6}"
                     f"{st['cash']:>14,.0f}{total:>14,.0f}"
                     f"{total_return_pct(st, total):>9.2f}%{held:>7}")
    lines.append("=" * 92)
    return "\n".join(lines)
