"""execute · Agent 决策落地器（B/C/D 组）。

三段式流水的第三段：

    prepare.py  →  decision_input.json
    [agent 回合]  →  decision_output.json          ← 不在本仓库内
    execute.py  →  校验 → 组合硬闸 → 风控 → 落账本 → 归档

设计要点：**规则候选只是参谋，agent 是决策者**，可采纳可否决；但 agent
无法绕过 broker 的硬校验（涨跌停/T+1/整手/资金）和本模块的组合限制
（单票权重、持仓数、市场状态下的新开仓额度）。

重构后，"推进一轮"的通用部分（互斥锁、跨日结算、交易时段、冷却去抖、
行情质量体检、风控加载、权益快照）全部由 `round_engine` 承担；
本模块只剩下 agent 特有的三件事：**读决策、验决策、把决策翻译成指令**。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from astock.pipeline.round_engine import OrderPlan, RoundContext, RoundReport, run_round
from astock.runtime import clock
from astock.runtime.paths import AccountPaths
from astock.strategy import signals

DECISION_LOG_COLUMNS = ["时间", "动作", "代码", "数量", "结果", "信息", "理由"]

#: 决策理由截断长度。agent 可能写很长，账本备注列不该被它撑爆。
REASON_MAX_LEN = 50


def default_group() -> str:
    """当前 agent 组。由 $ASTOCK_GROUP 决定，与 prepare/调度脚本约定一致。"""
    return os.environ.get("ASTOCK_GROUP", "B").strip().upper() or "B"


# ---------------------------------------------------------------------------
# 决策文件的校验
# ---------------------------------------------------------------------------

def validate_decision(raw: Any) -> tuple[bool, Any]:
    """逐条校验 agent 决策的结构。返回 (是否合法, 归一化结果 或 原因)。

    宽进严出：agent 写错一条不该让整个文件作废，但错的那条必须被显式报出来，
    不能静默丢弃——静默丢弃会让"agent 明明下了单却没成交"变成无从追查的怪事。
    """
    if not isinstance(raw, dict):
        return False, "非对象"
    action = str(raw.get("action", "")).lower().strip()
    if action not in ("buy", "sell"):
        return False, f"非法动作:{raw.get('action')}"
    code = str(raw.get("code", "")).strip()
    if not (code.isdigit() and len(code) == 6):
        return False, f"非法代码:{raw.get('code')}"
    try:
        qty = int(raw.get("qty", 0))
    except (TypeError, ValueError):
        return False, f"非法数量:{raw.get('qty')}"
    if qty <= 0:
        return False, f"数量<=0:{qty}"
    return True, {"action": action, "code": code, "qty": qty,
                  "reason": str(raw.get("reason", ""))[:REASON_MAX_LEN]}


def decision_freshness(paths: AccountPaths, raw: dict | None = None) -> tuple[bool, str]:
    """校验决策文件是否属于【本轮】。返回 (ok, 原因)。

    为什么必须有这道校验：execute 原先拿到 decision_output.json 就直接执行，
    对它是什么时候写的毫无判断。2026-08-23 实测：groupB/C/D 里躺着 08-20 写的
    决策文件（当时时区停摆，execute 从未消费、也就从未归档），一旦进入交易日
    就会把三天前的决策按今天的价格下单——与 07-31 时区事故同属"静默失效"家族。

    判据（任一不过即拒用，且必须大声说出来）：
      1) 必须存在本轮 decision_input.json —— 没有决策包就没有决策依据；
      2) decision_output 的 mtime 必须晚于 decision_input —— 否则是上轮残留；
      3) output 若带了 input_ts 字段，必须与本轮 decision_input 的 ts 一致
         （可选字段，带了就校验，是比 mtime 更强的溯源）。
    """
    in_path, out_path = paths.decision_input, paths.decision_output
    if not in_path.exists():
        return False, f"缺少本轮决策包 {in_path.name}，无法确认决策依据"
    try:
        out_m, in_m = out_path.stat().st_mtime, in_path.stat().st_mtime
    except OSError as exc:
        return False, f"读取文件时间失败：{exc}"
    if out_m <= in_m:
        age_h = (in_m - out_m) / 3600
        return False, (f"决策文件早于本轮决策包 {age_h:.1f}h，判为上一轮残留，拒绝执行"
                       f"（避免用旧决策按新价格下单）")
    if isinstance(raw, dict) and raw.get("input_ts"):
        try:
            current_ts = json.loads(in_path.read_text(encoding="utf-8")).get("ts")
        except (OSError, ValueError):
            current_ts = None
        if current_ts and str(raw["input_ts"]).strip() != str(current_ts).strip():
            return False, (f"决策文件声明的 input_ts={raw['input_ts']} "
                           f"与本轮决策包 ts={current_ts} 不符，拒绝执行")
    return True, ""


# ---------------------------------------------------------------------------
# decider
# ---------------------------------------------------------------------------

def _read_decisions(paths: AccountPaths, out) -> list[dict]:
    """读取并校验决策文件。任何一步不过都返回空列表——**宁可不交易**。"""
    if not paths.decision_output.exists():
        out(f"⚠ 未找到 agent 决策文件 {paths.decision_output.name}，本轮不下单（仅更新权益）。")
        return []
    try:
        raw = json.loads(paths.decision_output.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        out(f"⚠ 决策文件解析失败({exc!r}[:60])，本轮不下单。")
        return []

    fresh, why = decision_freshness(paths, raw=raw)
    if not fresh:
        out(f"🔴 决策文件未通过新鲜度校验，本轮不下单：{why}")
        return []

    if isinstance(raw, dict) and raw.get("comment"):
        out(f"agent 思路: {str(raw['comment'])[:200]}")
    return raw.get("decisions", []) if isinstance(raw, dict) else []


def _new_entry_budget(regime: str) -> int:
    """当前市场状态下本轮最多允许几笔新开仓。risk_off 一律不开新仓。"""
    if regime == "risk_off":
        return 0
    if regime == "high_volatility":
        return min(signals.MAX_NEW_PER_ROUND, 1)
    return signals.MAX_NEW_PER_ROUND


def _weight_capped_qty(ctx: RoundContext, code: str, wanted: int, price: float) -> int:
    """把买入数量压到单票权重上限之内。返回 0 表示已无额度。

    这是 agent **绕不过**的组合硬闸：agent 可以要求买 10 万股，
    但落到账本上的永远不会超过 MAX_WEIGHT 允许的仓位。
    """
    position = ctx.state.get("positions", {}).get(code, {})
    current_value = float(position.get("qty", 0)) * price
    room = float(ctx.equity) * signals.MAX_WEIGHT - current_value
    return int(max(0.0, room) / price // 100 * 100) if price > 0 else 0


def agent_decider(paths: AccountPaths, *, force: bool) -> Any:
    """构造 B/C/D 组的 decider：读决策文件 → 校验 → 施加组合硬闸 → 指令。"""

    def decide(ctx: RoundContext) -> OrderPlan:
        out = ctx.out
        out(f"  · 市场状态 {ctx.regime}")
        orders = []
        for raw in _read_decisions(paths, out):
            valid, result = validate_decision(raw)
            if not valid:
                out(f"  ✗ 跳过非法决策: {result} | 原始={raw}")
                continue

            code, qty = result["code"], result["qty"]
            quote = ctx.quotes.get(code)
            if not quote:
                out(f"  ✗ {code} 无行情，跳过")
                continue

            if result["action"] == "buy":
                price = float(quote.get("price") or 0)
                if price <= 0:
                    out(f"  ✗ {code} 无有效现价，跳过")
                    continue
                positions = ctx.state.get("positions", {})
                held = [p for p in positions.values() if p.get("qty", 0) > 0]
                if code not in positions and len(held) >= signals.MAX_POSITIONS:
                    out(f"  ✗ {code} 持仓已达上限 {signals.MAX_POSITIONS}，拒绝买入")
                    continue
                capped = _weight_capped_qty(ctx, code, qty, price)
                if capped <= 0:
                    out(f"  ✗ {code} 已达到单票权重上限，拒绝买入")
                    continue
                qty = min(qty, capped)

            group = paths.account
            tag = "[强制/非交易时段]" if force else ""
            orders.append({
                "action": result["action"], "code": code, "qty": qty,
                "reason": f"{tag}agent{group}:{result['reason']}",
            })
        return OrderPlan(orders=orders, max_new_buys=_new_entry_budget(ctx.regime))

    return decide


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def execute(group: str | None = None, *, verbose: bool = True,
            force: bool = False) -> RoundReport:
    """落地一个 agent 组的决策。

    force=True 只放行【交易时段】判断，用最近可得行情强制成交（测试/手动补轮次）。
    其余硬校验一律照旧，且成交备注会带 [强制/非交易时段] 标记，
    让账本自己说明这笔不是正常时段产生的。
    """
    group = (group or default_group()).upper()
    paths = AccountPaths.for_group(group).ensure_dirs()

    report = run_round(
        group,
        agent_decider(paths, force=force),
        config={"name": f"{group}组·agent决策"},
        force=force,
        verbose=verbose,
    )

    _log_decisions(paths, report)
    if report.ordered:
        _archive_decision(paths, report, verbose=verbose)
    return report


def _log_decisions(paths: AccountPaths, report: RoundReport) -> None:
    """把本轮成交写进 decision_log.csv，供事后核对 agent 说了什么、成了什么。"""
    from astock.core.ledger import append_csv_row
    stamp = clock.now().strftime("%H:%M:%S")
    for fill in report.fills:
        append_csv_row(paths.decision_log, DECISION_LOG_COLUMNS, [
            stamp, fill.side, fill.code, fill.qty, "成交",
            f"@{fill.price} 费用{fill.fee}", fill.reason,
        ])


def _archive_decision(paths: AccountPaths, report: RoundReport, *, verbose: bool) -> None:
    """归档已消费的决策文件。

    归档而非删除：决策与成交要能一一对上，事后才能重放核对
    "agent 当时看到什么、决定什么、最终成了什么"。
    """
    if not paths.decision_output.exists():
        return
    archive = paths.archived_decision(clock.now().strftime("%Y%m%d_%H%M%S"))
    Path(paths.decision_output).rename(archive)
    message = f"已归档本轮决策 -> {archive.name}"
    report.lines.append(message)
    if verbose:
        print(message)
