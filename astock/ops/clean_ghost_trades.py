"""
clean_ghost_trades · 清洗并发幽灵成交（幂等、可回滚、清完自检）
=====================================================================
背景：run_experiment/execute 曾是无锁的"读state→下单→写state"，两进程数秒内并发时
各自下单、后写覆盖先写 → trades.csv 多记了"幽灵行"，而 state.json 只留最后一次写的结果。
（根因已由 trade_guard 的执行层互斥+冷却去抖修复，本脚本只负责清理历史脏账。）

清洗判据（确定性、零主观）：
  1. 用 integrity_gate 找出同(代码,方向) 在 DUP_WINDOW_SEC 内重复的行；
  2. 对每组重复，**删较早的那一行、保留较晚的一行** —— 因为后写覆盖先写，
     state.json 记录的是"最后一次写"，且后续正常交易的现金链是接在存活行之上的；
  3. 删完重放 trades，其净持仓/现金必须与 state.json 完全一致（<1元容差），否则回滚。

安全：
  - 先备份 <file>.bak.<时间戳>；
  - 只有"清完后 integrity_gate 判 clean 且账实一致"才写回，否则保持原样并报错；
  - 幂等：已 clean 的账户直接跳过。

用法：
  python3 clean_ghost_trades.py            # 清洗 exp3/exp4/exp5（自动检测所有脏账户）
  python3 clean_ghost_trades.py --dry-run  # 只预演，不写盘
"""
import csv
import datetime as dt
import json
import os
import shutil
import sys

from astock.guards import integrity as ig
from astock.runtime import paths

BASE = str(paths.workspace())
WIN = ig.DUP_WINDOW_SEC


def _accounts():
    accts = [("A组", "state.json", "trades.csv")]
    for e in ("exp1", "exp2", "exp3", "exp4", "exp5"):
        accts.append((e, f"experiments/{e}_state.json", f"experiments/{e}_trades.csv"))
    accts.append(("B组", "groupB/state.json", "groupB/trades.csv"))
    return accts


def _read_trades(path):
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        rows = [dict(zip(header, line)) for line in r]
    return header, rows


def _parse(s):
    return ig._parse_ts(s)


def _ghost_row_indices(rows):
    """返回应删除的行下标集合：每组同(代码,方向)窗口内重复，删较早的一行。"""
    last = {}
    drop = set()
    for i, r in enumerate(rows):
        key = ((r.get("代码") or "").strip(), (r.get("方向") or "").strip())
        t = _parse(r.get("时间"))
        if not t:
            continue
        if key in last:
            pi, pt = last[key]
            if 0 <= (t - pt).total_seconds() <= WIN:
                drop.add(pi)          # 删较早那一行（先写被覆盖）
        last[key] = (i, t)
    return drop


def _replay(rows):
    qty = {}
    for r in rows:
        q = ig._i(r.get("数量"))
        side = (r.get("方向") or "").strip()
        if side == "买入":
            qty[r["代码"]] = qty.get(r["代码"], 0) + q
        elif side == "卖出":
            qty[r["代码"]] = qty.get(r["代码"], 0) - q
    return {k: v for k, v in qty.items() if v != 0}


def clean_account(name, spath, tpath, dry_run=False):
    spath = os.path.join(BASE, spath)
    tpath = os.path.join(BASE, tpath)
    if not (os.path.exists(spath) and os.path.exists(tpath)):
        return f"  {name}: 文件缺失，跳过"

    with open(spath, encoding="utf-8") as f:
        state = json.load(f)
    header, rows = _read_trades(tpath)

    before = ig.check(rows, state, init_cash=state.get("init_cash", 1_000_000.0))
    if before["clean"]:
        return f"  {name}: ✅ 本就 clean，无需清洗"

    drop = _ghost_row_indices(rows)
    if not drop:
        return (f"  {name}: 🔴 脏但未定位到窗口内重复行（可能是别类问题），"
                f"未做改动。红旗: {[f['check'] for f in before['red_flags']]}")

    kept = [r for i, r in enumerate(rows) if i not in drop]

    # 清完自检：重放净持仓必须等于 state
    replay_qty = _replay(kept)
    state_qty = {c: ig._i(p.get("qty")) for c, p in state.get("positions", {}).items()
                 if ig._i(p.get("qty")) != 0}
    after = ig.check(kept, state, init_cash=state.get("init_cash", 1_000_000.0))

    if replay_qty != state_qty or not after["clean"]:
        return (f"  {name}: 🔴 清洗后仍不一致，已回滚不写盘。\n"
                f"       重放持仓={replay_qty} vs state={state_qty}\n"
                f"       残留红旗={[f['check'] for f in after['red_flags']]}")

    msg = (f"  {name}: 定位幽灵行 {sorted(drop)}（删 {len(drop)} 行，保留 {len(kept)} 行）"
           f" → 清洗后 ✅ clean，账实一致")
    if dry_run:
        return msg + "  [dry-run 未写盘]"

    # 备份 + 写回
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"{tpath}.bak.{ts}"
    shutil.copy2(tpath, bak)
    with open(tpath, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in kept:
            w.writerow([r.get(h, "") for h in header])
    return msg + f"\n       已写回，原文件备份 -> {os.path.basename(bak)}"


def main():
    dry = "--dry-run" in sys.argv
    print("== 幽灵成交清洗" + ("（预演）" if dry else "") + " ==")
    for name, sp, tp in _accounts():
        print(clean_account(name, sp, tp, dry_run=dry))
    print("\n== 清洗后复检 ==")
    os.system(f'python3 "{os.path.join(BASE, "integrity_gate.py")}"')


if __name__ == "__main__":
    main()
