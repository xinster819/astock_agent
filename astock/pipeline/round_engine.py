"""round_engine · 一次交易轮的**唯一**编排实现。

【它取代了什么】
重构前，"推进一轮"这件事在仓库里有三份逐字复制的实现：

    run.py       A 组（纯规则对照基线）
    run_exp.py   exp1~exp9（规则实验组）
    execute.py   B/C/D 组（agent 决策落地）

三份都做同样的十来步：钉时区 → 清指标缓存 → 判交易时段 → 跨日结算 →
取行情 → 记价差 → 行情质量体检 → 加载风控 → 生成/读取指令 → 先卖后买 →
推进轮次 → 写权益快照 → 落盘。复制带来的不是重复劳动，而是**发散**：

  · `run.py` 没有账户互斥锁，也没有冷却去抖——而"幽灵成交"正是靠这两样根治的，
    A 组一直裸奔。
  · `run_exp.py` 自带一份 `_buy_exp/_sell_exp/_calc_buy_fee`，费率写成字面量
    `1.0026`（真值 1.00026，差了一个数量级），且只有它会写 `opened_at`，
    于是时间止损在 A/B/C/D 组永远不触发。
  · 三处的行情质量体检输出格式各不相同，报警文案对不上。

现在只有这一份。**账户之间的真实差异收敛到两处**：`decide` 回调（指令从哪来）
和 `RoundPolicy`（启用哪些闸门）。

【为什么闸门要做成 policy 而不是一律打开】
A 组是**对照基线**，整个项目的价值就在于它与其他组的可比性。给它补上组合风控
会改变它的成交行为，等于把对照组也变成实验组——那是在破坏实验，不是在修 bug。
所以：
  · 账户互斥锁、冷却去抖 —— 对所有账户启用。它们只拦"同一轮被触发两次"，
    不改变任何策略语义，纯粹是防止账本损坏。
  · 组合风控（日内亏损限/回撤/连亏冷却）—— 按 policy，A 组默认关闭，
    保持它"纯规则"的基线语义不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from astock.core.account import Account
from astock.core.rules import Fill
from astock.data import market
from astock.guards import risk as risk_guard
from astock.guards import trade as trade_guard
from astock.runtime import clock
from astock.strategy import signals


@dataclass
class OrderPlan:
    """本轮要下的指令，外加只有下单时才能判定的额度限制。

    `max_new_buys` 必须在这里、而不是在 decider 里自行截断——因为它约束的是
    **成功成交**的笔数：前面的买单若被涨跌停/资金拒掉，后面的候选应当补位。
    decider 拿不到成交结果，只有 `_place` 能边下边数。
    """

    orders: list = field(default_factory=list)
    max_new_buys: int | None = None      # None = 不限

    @classmethod
    def of(cls, value) -> OrderPlan:
        """允许 decider 直接返回指令列表（规则组用不到额度限制）。"""
        return value if isinstance(value, cls) else cls(orders=list(value))


#: 决策回调：拿到本轮上下文，返回指令列表或 `OrderPlan`。
#: 指令格式 {"action": "buy"|"sell", "code": str, "qty": int, "reason": str}
Decider = Callable[["RoundContext"], "OrderPlan | list"]


@dataclass(frozen=True)
class RoundPolicy:
    """一个账户启用哪些闸门。默认值 = 实验组/Agent 组的完整配置。"""

    #: 冷却去抖秒数。距上次成功执行不足此值判为重复触发。None 表示不启用。
    cooldown_sec: int | None = trade_guard.DEFAULT_COOLDOWN_SEC
    #: 是否启用组合风控。A 组（对照基线）关闭以保持可比性。
    use_risk_guard: bool = True
    #: 是否顺带采集沪深300 双源价差样本（仅用于校准阈值，不参与下单）。
    sample_spreads: bool = True

    @classmethod
    def control_group(cls) -> RoundPolicy:
        """A 组：纯规则对照基线，不加组合风控。"""
        return cls(use_risk_guard=False)


@dataclass
class RoundContext:
    """本轮的全部已知事实，传给 `decide` 回调。"""

    account: Account
    quotes: dict[str, Any]
    config: dict[str, Any]
    regime: str
    equity: float
    force: bool
    now: datetime
    out: Callable[[str], None]

    @property
    def state(self) -> dict[str, Any]:
        return self.account.state

    def signal_config(self) -> dict[str, Any]:
        """给 strategy 用的配置：实验组参数 + 本轮实测市场状态。"""
        cfg = dict(self.config)
        cfg["market_regime"] = self.regime
        return cfg


@dataclass
class RoundReport:
    """一轮的结果。`ordered` 区分"跑完了"和"真的进了下单分支"——

    这个区分是 2026-07-31 停摆事故的直接产物：当时进程照常运行、权益曲线照常写，
    唯独从未进入下单分支，而所有闸门都看不出异常。`freshness_gate.stalled_engine`
    依赖 state 里的 `last_trading_round_date`，而那个字段只在 ordered=True 时推进。
    """

    account: str
    lines: list[str] = field(default_factory=list)
    ordered: bool = False
    skipped: str | None = None
    total: float = 0.0
    return_pct: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def run_round(
    account_id: str,
    decide: Decider,
    *,
    config: dict[str, Any] | None = None,
    policy: RoundPolicy | None = None,
    init_cash: float | None = None,
    force: bool = False,
    verbose: bool = True,
    now: datetime | None = None,
) -> RoundReport:
    """推进 `account_id` 的一轮交易。

    force=True 跳过【交易时段】判断，用最近可得行情强制成交（测试/手动补轮次）。
    ⚠ 只放行时段判断——涨跌停、T+1、整手、资金、组合限制、风控一律照旧。
    """
    policy = policy or RoundPolicy()
    config = config or {}
    report = RoundReport(account=account_id)

    def out(message: str) -> None:
        report.lines.append(message)
        if verbose:
            print(message)

    clock.enforce()                    # 幂等：写账本前把进程时区钉死为交易所时区
    signals.clear_indicator_cache()    # 轮首清缓存，保证本轮取到最新日线

    # 整段"读 state → 下单 → 写 state"必须串行化，否则两进程各下一单、
    # 后写覆盖先写 —— 这就是"幽灵成交"。
    try:
        with trade_guard.account_lock(account_id):
            return _run_locked(account_id, decide, config, policy,
                               init_cash, force, now, out, report)
    except trade_guard.LockBusy as exc:
        report.skipped = str(exc)
        out(f"⏭ 跳过本轮：{exc}")
        return report


def _run_locked(account_id, decide, config, policy, init_cash,
                force, now, out, report) -> RoundReport:
    """持锁后的核心流程。状态必须在锁内加载，才能看到上一执行者落盘的时间戳。"""
    account = Account.open(account_id, init_cash=init_cash)
    now = now or datetime.now()
    trading, status = market.is_trading_now(now)
    label = config.get("name", account_id)
    out(f"=== [{account_id}] {label} | {now.strftime('%Y-%m-%d %H:%M:%S')} | 市场: {status} ===")

    if account.settle_new_day():
        out("跨日结算：T+1 冻结份额已解冻为可用。")

    # ---- 非交易时段：只刷估值，不下单 ----
    if not trading and not force:
        return _idle(account, out, report, "非交易时段，跳过下单")

    # ---- 冷却去抖：距上次成功执行不足冷却期 → 判为重复触发 ----
    if policy.cooldown_sec is not None:
        ok, why = trade_guard.can_execute(
            account.state, now=now.timestamp(), cooldown_sec=policy.cooldown_sec)
        if not ok:
            # 不下单但仍刷估值；last_run_ts 保持不变，防重触发的时间戳不被延后
            report.skipped = why
            return _idle(account, out, report, f"⏭ 跳过下单（防抖）：{why}")

    quotes = _fetch_quotes(account, out, policy)
    equity = account.market_value(quotes)[1]
    guard = _prepare_risk_guard(account.state, config, equity, now,
                                enabled=policy.use_risk_guard)

    regime = _current_regime(config, out)
    ctx = RoundContext(account=account, quotes=quotes, config=config, regime=regime,
                       equity=equity, force=force, now=now, out=out)

    plan = OrderPlan.of(decide(ctx))
    out(f"生成指令 {len(plan.orders)} 条")
    _place(account, plan, quotes, guard, out, report)

    if guard is not None:
        account.state["risk_guard"] = guard.to_dict()
    account.state["market_regime"] = regime

    # 走完下单分支才推进轮次标记。`last_trading_round_date` 是
    # freshness_gate.stalled_engine 判定"引擎停摆"的唯一依据——
    # 判据必须是"是否进入下单分支"，而不是"是否有成交"：
    # 高门槛策略长期无信号是正常的，长期不进下单分支才是故障。
    account.state["round"] = account.state.get("round", 0) + 1
    account.state["last_trading_round_date"] = clock.today()
    report.ordered = True

    total, ret = account.snapshot_equity(quotes)
    account.save()
    report.total, report.return_pct = total, ret
    out(f"本轮结束 #{account.state['round']}。总资产 {total:,.2f}，"
        f"累计收益 {ret}%，现金 {account.state['cash']:,.2f}")
    return report


def _idle(account: Account, out, report: RoundReport, message: str) -> RoundReport:
    """不下单的收尾：仍要刷新估值并落盘，让权益曲线保持连续。"""
    pool = set(signals.load_pool()) | set(account.state["positions"])
    quotes = market.get_quotes(list(pool))
    total, ret = account.snapshot_equity(quotes)
    account.save()
    report.total, report.return_pct = total, ret
    out(f"{message}。当前总资产 {total:,.2f}，累计收益 {ret}%")
    return report


def _fetch_quotes(account: Account, out, policy: RoundPolicy) -> dict[str, Any]:
    """取全池 + 持仓的实时行情，并做质量体检。

    体检必须**显式报告**：多源分歧/取价失败的票会被 broker 以 price=0 拒单，
    如果不报出来，表现就是"策略今天没开仓"——与真的没信号无法区分，
    又一种静默失效。
    """
    codes = list(set(signals.load_pool()) | set(account.state["positions"]))
    quotes = market.get_quotes(codes)

    market.log_spread(quotes)
    if policy.sample_spreads:
        try:
            n, _ = market.sample_spreads()
            if n:
                out(f"价差采样：沪深300 采集 {n} 只有效双源样本，已记入 spread_log.csv")
        except Exception as exc:                       # 采样失败不该拖垮交易
            out(f"价差采样跳过：{exc!r}"[:100])

    bad, degraded = [], []
    for code, quote in quotes.items():
        if quote.get("error") or quote.get("dirty") or quote.get("diverge") \
                or (quote.get("price") or 0) <= 0:
            why = (quote.get("diverge") or quote.get("dirty")
                   or quote.get("error") or "现价0(休市/拉取失败)")
            bad.append(f"{code}({why})")
        elif str(quote.get("cross", "")).startswith("single_source"):
            degraded.append(code)

    if bad:
        out(f"⚠ 行情异常 {len(bad)}/{len(quotes)} 只，本轮跳过：" + "; ".join(bad[:5]))
    if degraded:
        out(f"⚠ 单源降级 {len(degraded)} 只（仅1源可用，已放行但风险提高）："
            + ", ".join(degraded[:10]))
    return quotes


def _current_regime(config: dict[str, Any], out) -> str:
    """市场状态：live → last-known-good 缓存 → 冷启动默认，降级会显式说出来。"""
    from astock.guards import regime as regime_mod
    fallback = config.get("market_regime_fallback", "risk_off")
    result = regime_mod.classify(cold_start_default=fallback)
    if result.degraded:
        out(f"  ⚠ 市场状态降级[{result.source}]：{result.detail}")
    return result.regime


def _prepare_risk_guard(state, config, equity, now, *, enabled: bool):
    """加载组合风控并刷新当日/峰值权益基准。enabled=False 时只刷基准不建闸门。

    即便不启用风控，`day_start_equity` / `peak_equity` 也照常维护——
    报表和事后分析都要用，且日后想给某个账户打开风控时基准是连续的。
    """
    today = now.strftime("%Y-%m-%d")
    if state.get("risk_date") != today:
        state["risk_date"] = today
        state["day_start_equity"] = equity
    state["day_start_equity"] = float(state.get("day_start_equity", equity))
    state["peak_equity"] = max(float(state.get("peak_equity", equity)), equity)

    if not enabled:
        return None

    cfg = config.get("risk_guard", {}) if isinstance(config, dict) else {}
    guard = risk_guard.RiskGuard(
        daily_loss_limit=cfg.get("daily_loss_limit", 0.02),
        max_drawdown=cfg.get("max_drawdown", 0.10),
        consecutive_loss_limit=cfg.get("consecutive_loss_limit", 3),
        loss_cooldown_trades=cfg.get("loss_cooldown_trades", 3),
        stop_loss_cooldown_trades=cfg.get("stop_loss_cooldown_trades", 3),
    )
    guard.restore(state.get("risk_guard"))
    return guard


def _place(account: Account, plan: OrderPlan, quotes, guard, out,
           report: RoundReport) -> None:
    """先卖后买地执行指令。卖出腾出的现金本轮即可用于买入。"""
    ordered = sorted(plan.orders, key=lambda o: 0 if o["action"] == "sell" else 1)
    new_buys = 0
    for order in ordered:
        quote = quotes.get(order["code"])
        if not quote:
            out(f"  ✗ {order['code']} 无行情快照，跳过")
            continue

        if order["action"] == "buy":
            if plan.max_new_buys is not None and new_buys >= plan.max_new_buys:
                out(f"  ✗ {order['code']} 已达本轮新开仓上限 {plan.max_new_buys}")
                continue
            if guard is not None:
                decision = guard.allow(
                    account.market_value(quotes)[1],
                    account.state["day_start_equity"],
                    account.state["peak_equity"],
                    order["code"],
                )
                if not decision.allowed:
                    out(f"  ✗ {order['code']} 风控拒绝买入：{decision.reason}")
                    continue
            result = account.buy(quote, order["qty"], order.get("reason", ""))
            if result.ok:
                new_buys += 1
        else:
            result = account.sell(quote, order["qty"], order.get("reason", ""))
            if result.ok and guard is not None and result.fill is not None:
                # 用 Fill 里的真实已实现盈亏喂风控。重构前这里是拿卖出【之前】
                # 的持仓成本和【请求】数量估出来的，与实际成交量不一定一致。
                guard.record_trade(
                    result.fill.realized_pnl or 0.0,
                    order["code"],
                    stop_loss="止损" in order.get("reason", ""),
                )

        if result.ok and result.fill is not None:
            report.fills.append(result.fill)
        out(("  ✓ " if result.ok else "  ✗ ") + result.message)


# ---------------------------------------------------------------------------
# 现成的 decider
# ---------------------------------------------------------------------------

def rule_decider(ctx: RoundContext) -> list:
    """规则组的决策：直接跑 strategy 的信号引擎。A 组与 exp1~exp9 共用。"""
    return signals.generate_signals(ctx.state, ctx.quotes, exp_config=ctx.signal_config())
