"""account · 账户门面：把 `rules`（纯规则）和 `ledger`（落盘）组合成可下单的对象。

【它替代了什么】
重构前有两套并行的账本读写：

    broker.load_state / save_state / buy / sell        A 组与 B/C/D 组走这条
    exp_manager.load_exp_state / save_exp_state        exp1~exp9 走这条

两者做的是同一件事，却各写各的初始化逻辑（初始现金字段名、created 日期取法、
是否带 exp_id 都不一致），且都靠模块级全局路径工作。`run_exp` 之所以要绕开
broker 自建一套，根因就是 broker 在 **import 期**读 `ASTOCK_GROUP` 把路径钉死，
一个进程内没法碰第二个账户。

`Account` 把路径变成构造参数：同一进程可以同时打开 13 个账户，
测试可以在临时目录里开账户而不碰任何环境变量。两套读写就此合并为一套。

【时钟】
所有日期/时间戳一律取自 `runtime.clock`（交易所时区），不再用裸
`datetime.now()`。2026-07-31 停摆事故的根因就是进程时区与交易所时区不一致；
`clock.enforce()` 是第一层防御，这里直接用显式时钟是第二层——
即使某个新入口忘了调 enforce()，账本日期也不会错位。
"""
from __future__ import annotations

from typing import Any

from astock.core import rules
from astock.core.fees import INIT_CASH
from astock.core.ledger import Ledger
from astock.core.rules import Execution
from astock.runtime import clock
from astock.runtime.paths import AccountPaths


class Account:
    """一个虚拟账户。持有内存中的 state，显式 `save()` 才落盘。

    典型用法：

        acct = Account.open("exp1", init_cash=1_000_000)
        acct.settle_new_day()
        acct.buy(quote, 500, reason="上穿MA20")
        acct.snapshot_equity(quotes)
        acct.save()
    """

    def __init__(self, paths: AccountPaths, state: dict[str, Any],
                 ledger: Ledger | None = None) -> None:
        self.paths = paths
        self.state = state
        self.ledger = ledger or Ledger(paths)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, account: str | None = None, *, init_cash: float | None = None,
             extra: dict[str, Any] | None = None) -> Account:
        """打开账户；账本不存在则按 init_cash 初始化并立即落盘。

        account 省略时读 `$ASTOCK_GROUP`（默认 A 组），保持与调度脚本的既有约定。
        """
        paths = AccountPaths.for_account(account)
        ledger = Ledger(paths)
        if ledger.state_exists():
            return cls(paths, ledger.load_state(), ledger)

        paths.ensure_dirs()
        state = cls.initial_state(
            init_cash=INIT_CASH if init_cash is None else float(init_cash),
            extra=extra,
        )
        ledger.save_state(state)
        return cls(paths, state, ledger)

    @staticmethod
    def initial_state(init_cash: float = INIT_CASH,
                      extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """全新账户的初始状态。**唯一**一份初始化逻辑。"""
        today = clock.today()
        state: dict[str, Any] = {
            "cash": init_cash,
            "init_cash": init_cash,
            "positions": {},          # code -> {qty, available, cost, name}
            "created": today,
            "last_settle_date": today,
            "round": 0,
        }
        if extra:
            state.update(extra)
        return state

    def save(self) -> None:
        self.ledger.save_state(self.state)

    def reload(self) -> Account:
        """从磁盘重读 state。持锁后调用，确保看到上一执行者已落盘的时间戳。"""
        if self.ledger.state_exists():
            self.state = self.ledger.load_state()
        return self

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------

    def settle_new_day(self) -> bool:
        """跨日结算（T+1 解冻）。返回是否真的发生了结算。"""
        return rules.settle_new_day(self.state, clock.today())

    def buy(self, quote: dict[str, Any], qty: int, reason: str = "") -> Execution:
        return self._execute(rules.buy(self.state, quote, qty, reason), quote)

    def sell(self, quote: dict[str, Any], qty: int, reason: str = "") -> Execution:
        return self._execute(rules.sell(self.state, quote, qty, reason), quote)

    def _execute(self, result: Execution, quote: dict[str, Any]) -> Execution:
        """成交才写账本。拒单不留痕——trades.csv 是成交流水，不是尝试日志。

        时间列记的是**成交时刻**，不是行情快照的取价时刻。

        ⚠ 这是一处行为修正。旧实现用 `quote["ts"]`——那是逐只股票取价时打的
        时间戳，而下单顺序（先卖后买、买入再按候选分排序）与取价顺序不同，
        于是成交行的时间**不单调**。真实账本里有 12 行是倒序的。
        后果不只是难看：`integrity.duplicate_order` 判重时算的是
        `gap = ts - 上一次同票同向的 ts`，只在 `0 <= gap <= 120s` 时告警——
        倒序产生的负 gap 被直接跳过。也就是说**幽灵成交检测器会漏掉
        时间戳恰好倒序的那一半**，而它存在的全部理由就是抓幽灵成交。
        用成交时刻则天然单调。
        """
        if result.ok and result.fill is not None:
            self.ledger.append_fill(result.fill, timestamp=clock.stamp())
        return result

    # ------------------------------------------------------------------
    # 估值
    # ------------------------------------------------------------------

    def market_value(self, quotes: dict[str, Any]) -> tuple[float, float]:
        return rules.market_value(self.state, quotes)

    def snapshot_equity(self, quotes: dict[str, Any], *, write: bool = True) -> tuple[float, float]:
        """算总资产与累计收益率，可选写入 equity.csv。

        无论是否写盘都会推进 `peak_equity`——最大回撤风控依赖它，
        漏更新会让回撤显得比实际小，闸门因此失灵。
        """
        mv, total = self.market_value(quotes)
        ret = rules.total_return_pct(self.state, total)
        self.state["peak_equity"] = max(float(self.state.get("peak_equity", total)), total)
        if write:
            self.ledger.append_equity(clock.stamp(), self.state["cash"], mv, total, ret)
        return total, ret

    # ------------------------------------------------------------------

    @property
    def account_id(self) -> str:
        return self.paths.account

    def __repr__(self) -> str:
        return (f"<Account {self.paths.account} cash={self.state.get('cash')} "
                f"positions={len(self.state.get('positions', {}))}>")
