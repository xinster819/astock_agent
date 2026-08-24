"""clean_ghost_trades · 清洗历史遗留的并发幽灵成交（幂等、可回滚、清完自检）。

【背景】
下单流程曾是无锁的"读 state → 下单 → 写 state"，两进程数秒内并发时各自下单、
后写覆盖先写 → trades.csv 多记了「幽灵行」，而 state.json 只留最后一次写的结果。
根因已由 `guards.trade` 的执行层互斥 + 冷却去抖修复，本脚本只负责清理历史脏账。

【清洗判据（确定性、零主观）】
  1. 找出同 (代码, 方向) 在 `DUP_WINDOW_SEC` 内重复的行；
  2. 对每组重复**删较早的一行、保留较晚的一行**——因为后写覆盖先写，
     state.json 记的是最后一次写，且后续正常交易的现金链接在存活行之上；
  3. 删完重放 trades，净持仓必须与 state.json 完全一致，否则**回滚不写盘**。

【安全】
  · 先备份 `<file>.bak.<时间戳>`
  · 只有"清完后闸门判 clean 且账实一致"才写回
  · 幂等：已 clean 的账户直接跳过
  · `--dry-run` 只预演

【重构中修掉的四个问题】
  1. 账户表只列了 A / exp1~exp5 / B 共 6 个，**exp6~exp9 与 C/D 组的幽灵成交
     永远清不到**。这是同一份账户名单在仓库里的第五处硬编码。现在走 `paths.all_accounts()`。
  2. `main()` 用 `os.system("python3 .../integrity_gate.py")` 做清洗后复检——
     那个文件在分包后已不存在，复检**静默什么也没做**。何况它调的是系统 python3
     而非虚拟环境。现在直接调用 `integrity.check`。
  3. `BASE` 是模块级常量，import 期就把工作区钉死，测试无法重定向。
  4. 判重用 `0 <= gap`，账本时间戳倒序时会漏判——与 `guards.integrity` 同源的问题。
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from astock.guards import integrity
from astock.runtime import files, paths
from astock.runtime.paths import AccountPaths

WINDOW_SEC = integrity.DUP_WINDOW_SEC


@dataclass
class CleanResult:
    """一个账户的清洗结果。`changed` 为真才动过磁盘。"""

    account: str
    message: str
    changed: bool = False
    dropped: list[int] | None = None
    backup: Path | None = None

    def __str__(self) -> str:
        return f"  {self.account}: {self.message}"


def ghost_row_indices(rows: list[dict]) -> set[int]:
    """返回应删除的行下标：每组窗口内重复，删较早的一行。

    间隔取绝对值——账本里的成交时间戳可能倒序（历史上时间列记的是取价时刻
    而非成交时刻），只判 `0 <= gap` 会漏掉倒序的那一半重复行。
    """
    last: dict[tuple[str, str], tuple[int, dt.datetime]] = {}
    drop: set[int] = set()
    for index, row in enumerate(rows):
        key = ((row.get("代码") or "").strip(), (row.get("方向") or "").strip())
        at = integrity._parse_ts(row.get("时间"))
        if not at:
            continue
        if key in last:
            prev_index, prev_at = last[key]
            if abs((at - prev_at).total_seconds()) <= WINDOW_SEC:
                drop.add(min(prev_index, index))   # 删较早写入的那一行
        last[key] = (index, at)
    return drop


def replay_positions(rows: list[dict]) -> dict[str, int]:
    """重放成交流水，算出净持仓。数量为 0 的票不计入。"""
    qty: dict[str, int] = {}
    for row in rows:
        code = (row.get("代码") or "").strip()
        side = (row.get("方向") or "").strip()
        amount = integrity._i(row.get("数量"))
        if side == "买入":
            qty[code] = qty.get(code, 0) + amount
        elif side == "卖出":
            qty[code] = qty.get(code, 0) - amount
    return {code: n for code, n in qty.items() if n != 0}


def _state_positions(state: dict) -> dict[str, int]:
    return {code: integrity._i(p.get("qty"))
            for code, p in (state.get("positions") or {}).items()
            if integrity._i(p.get("qty")) != 0}


def clean_account(account_paths: AccountPaths, *, dry_run: bool = False) -> CleanResult:
    """清洗一个账户。只有清完自检通过才写盘，否则原样保留并说明原因。"""
    name = account_paths.account
    state = files.read_json(account_paths.state)
    if state is None or not account_paths.trades.exists():
        return CleanResult(name, "文件缺失，跳过")

    rows = files.read_csv_rows(account_paths.trades)
    init_cash = state.get("init_cash", 1_000_000.0)

    before = integrity.check(rows, state, init_cash=init_cash)
    if before["clean"]:
        return CleanResult(name, "✅ 本就 clean，无需清洗")

    drop = ghost_row_indices(rows)
    if not drop:
        flags = [f["check"] for f in before["red_flags"]]
        return CleanResult(name, f"🔴 脏但未定位到窗口内重复行（可能是别类问题），"
                                 f"未做改动。红旗: {flags}")

    kept = [row for index, row in enumerate(rows) if index not in drop]

    # ---- 清完自检：重放净持仓必须等于 state，且闸门必须判 clean ----
    after = integrity.check(kept, state, init_cash=init_cash)
    replayed, declared = replay_positions(kept), _state_positions(state)
    if replayed != declared or not after["clean"]:
        return CleanResult(name, (
            f"🔴 清洗后仍不一致，已回滚不写盘。\n"
            f"       重放持仓={replayed} vs state={declared}\n"
            f"       残留红旗={[f['check'] for f in after['red_flags']]}"))

    message = (f"定位幽灵行 {sorted(drop)}（删 {len(drop)} 行，"
               f"保留 {len(kept)} 行）→ 清洗后 ✅ clean，账实一致")
    if dry_run:
        return CleanResult(name, message + "  [dry-run 未写盘]", dropped=sorted(drop))

    backup = _rewrite_trades(account_paths.trades, kept)
    return CleanResult(name, message + f"\n       已写回，原文件备份 -> {backup.name}",
                       changed=True, dropped=sorted(drop), backup=backup)


def _rewrite_trades(path: Path, rows: list[dict]) -> Path:
    """备份后整体重写 trades.csv。备份**必须先落盘**，否则无从回滚。"""
    from astock.core.ledger import TRADE_COLUMNS

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{stamp}")
    backup.write_bytes(path.read_bytes())

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(TRADE_COLUMNS)
        for row in rows:
            writer.writerow([row.get(column, "") for column in TRADE_COLUMNS])
    return backup


def clean_all(*, dry_run: bool = False, printer=print) -> list[CleanResult]:
    """遍历全部 13 个账户。账户名单来自 `paths.all_accounts()`，不再手写。"""
    printer("== 幽灵成交清洗" + ("（预演）" if dry_run else "") + " ==")
    results = [clean_account(ap, dry_run=dry_run) for ap in paths.all_accounts()]
    for result in results:
        printer(str(result))

    printer("\n== 清洗后复检 ==")
    for account_paths in paths.all_accounts():
        state = files.read_json(account_paths.state)
        if state is None:
            continue
        rows = files.read_csv_rows(account_paths.trades)
        gate = integrity.check(rows, state, init_cash=state.get("init_cash", 1_000_000.0))
        mark = "✅ clean" if gate["clean"] else f"🔴 {len(gate['red_flags'])} 红旗"
        printer(f"  {account_paths.account}: {mark}")
    return results
