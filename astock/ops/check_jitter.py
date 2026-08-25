"""
抖动核对 + 全组产出健康度核对：
  A) 主账户(A组)：深度核对——抖动是否生效、是否被超时截断、链路是否自洽。
     依据 jitter_log.csv（仅 A组 run.py 写）+ equity.csv + spread_log.csv。
  B) 实验组(exp1~8) + B组：产出健康度核对——本整点是否产出新权益行。
     这些组用 --no-jitter，不写 jitter_log，故只能以 equity.csv 是否出现
     "本整点新行"来判定：有新行=本轮跑通并落账；无新行=被截断/未触发/非交易时段。

用法：python3 check_jitter.py [HH]   # HH 可选，默认核对最近一个整点

A组三路交叉判据（互为佐证）：
  1) jitter_log.csv：同一'唤醒时刻'应有 sleeping + fired 两行。
     - 只有 sleeping 无 fired  -> 进程睡眠中被杀（超时截断），抖动失败。
     - 有 fired，且 实际延时≈计划延时(±3s) -> 抖动真实生效。
  2) equity.csv / spread_log.csv：本轮实跑产物的时间戳。
     - 时间戳 = 整点 + 抖动 + 实跑耗时，应落在整点后 1~10 分钟内。
     - 若该整点完全没有新行 -> 本轮未产出（截断或未触发）。
  3) 三者时间戳应自洽：fired时刻 < equity时间戳，差值≈实跑耗时(约20s)。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from astock.reporting import roster
from astock.runtime import files, paths

#: A 组抖动生效时，equity 产物应落在整点后的窗口（分钟）
CONTROL_WINDOW_MIN = (0, 10)
#: 其余各组以 --no-jitter 串行触发，累计耗时更长，窗口相应放宽
GROUP_WINDOW_MIN = (0, 20)
#: 计划延时与实际延时的允许偏差（秒）
JITTER_TOLERANCE_SEC = 3


def _parse_hms(s):
    try:
        return dt.datetime.strptime(s.strip(), "%H:%M:%S").time()
    except Exception:
        return None


def _hour_of(ts_str):
    # equity/spread 用 'YYYY-MM-DD HH:MM:SS'
    try:
        return dt.datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


@dataclass(frozen=True)
class JitterVerdict:
    """一个整点的抖动判定。`truncated` 是唯一真正要紧的结论。"""

    level: str            # 高 / 中 / 低
    message: str
    truncated: bool = False


def pair_jitter_rows(rows: list[dict], target_hour: int) -> dict[str, dict[str, dict]]:
    """按「唤醒时刻」把 sleeping / fired 两行配对，只保留目标整点的。

    配对是全部判据的基础：同一次唤醒应当有两行。
    """
    paired: dict[str, dict[str, dict]] = {}
    for row in rows:
        wake_raw = row.get("唤醒时刻", "")
        wake = _parse_hms(wake_raw)
        if not wake or wake.hour != target_hour:
            continue
        paired.setdefault(wake_raw, {})[row.get("状态", "")] = row
    return paired


def orphan_sleeps(paired: dict[str, dict[str, dict]]) -> list[str]:
    """只有 sleeping、没有 fired 的唤醒时刻 —— 进程在睡眠中被杀掉了。

    这是 jitter_log 存在的全部理由：没有这对行，超时截断只会表现为
    「那一轮什么都没发生」，与「非交易时段正常跳过」无法区分。
    """
    return sorted(w for w, ev in paired.items() if "sleeping" in ev and "fired" not in ev)


def judge_jitter(paired: dict[str, dict[str, dict]]) -> JitterVerdict:
    """对最近一次唤醒下判定。"""
    if not paired:
        return JitterVerdict("中", "该整点窗口内 jitter_log 无记录——可能未触发，或用了 --no-jitter。")

    latest = paired[sorted(paired)[-1]]
    if "fired" not in latest:
        row = latest["sleeping"]
        planned = float(row.get("计划延时s") or 0)
        return JitterVerdict("高", (
            f"⚠ 检测到超时截断：{row.get('唤醒时刻')} 进入 {planned:.0f}s 睡眠后无 fired 行"
            f"——进程在睡眠中被杀。需调大执行端 timeout。"), truncated=True)

    row = latest["fired"]
    planned = float(row.get("计划延时s") or 0)
    actual = float(row.get("实际延时s") or 0)
    diff = abs(planned - actual)
    if diff <= JITTER_TOLERANCE_SEC:
        return JitterVerdict("高", (
            f"抖动真实生效：计划{planned:.0f}s vs 实际{actual:.0f}s"
            f"（差{diff:.1f}s≤{JITTER_TOLERANCE_SEC}s），"
            f"{row.get('唤醒时刻')}唤醒→{row.get('实际开跑时刻')}开跑。"))
    return JitterVerdict("中", (
        f"已开跑但延时偏差大：计划{planned:.0f}s vs 实际{actual:.0f}s（差{diff:.1f}s），"
        f"可能调度器睡眠期间有挂起/暂停。"))


def latest_equity_in_hour(path: Path, target_hour: int,
                          day: dt.date) -> tuple[dt.datetime, str, str] | None:
    """该权益账本在指定整点、指定**日期**的最后一行。

    ⚠ 必须比完整日期。旧实现只比日号（几号），每月同一日号会把上个月的旧行
    当成「本轮产物」，恰恰在停摆故障上发出虚假绿灯。
    """
    best = None
    for row in files.read_csv_rows(path):
        at = _hour_of(row.get("时间", ""))
        if at and at.hour == target_hour and at.date() == day \
                and (not best or at > best[0]):
            best = (at, row.get("总资产", ""), row.get("累计收益率%", ""))
    return best


def delay_minutes(at: dt.datetime) -> float:
    """产出时刻距该整点过了多少分钟。"""
    return (at - at.replace(minute=0, second=0, microsecond=0)).total_seconds() / 60


def check(target_hour: int | None = None, *, now: dt.datetime | None = None,
          printer=print) -> tuple[list, list]:
    """A 组抖动深度核对 + 其余各组产出健康度核对。"""
    now = now or dt.datetime.now()
    target_hour = now.hour if target_hour is None else target_hour
    printer(f"=== A组·抖动+截断深度核对 | 目标整点 {target_hour:02d}:00 "
            f"| 现在 {now.strftime('%H:%M:%S')} ===")

    jitter_rows = files.read_csv_rows(paths.jitter_log())
    verdict: list[tuple[str, str]] = []

    if not jitter_rows:
        verdict.append(("低", "jitter_log.csv 不存在或为空——可能本轮用了 --no-jitter，或尚未触发。"))
        paired: dict = {}
    else:
        paired = pair_jitter_rows(jitter_rows, target_hour)
        judgement = judge_jitter(paired)
        verdict.append((judgement.level, judgement.message))
        orphans = orphan_sleeps(paired)
        if orphans:
            verdict.append(("中", f"另发现 {len(orphans)} 个孤儿睡眠记录"
                                  f"（唤醒于 {', '.join(orphans)}）"
                                  f"——历史上有过被截断的轮次，建议复核。"))

    # ---- equity / spread：该整点是否有新产物 ----
    control = roster.by_account()["A"]
    equity_point = latest_equity_in_hour(control.paths.equity, target_hour, now.date())
    lo, hi = CONTROL_WINDOW_MIN
    if equity_point:
        at = equity_point[0]
        delay = delay_minutes(at)
        ok = lo < delay <= hi
        verdict.append(("高" if ok else "中",
                        f"equity.csv 本轮产物 {at.strftime('%H:%M:%S')}（整点后{delay:.1f}分钟）"
                        + (f"，落在{lo}~{hi}分钟合理窗口，未被截断。" if ok
                           else "，超出预期窗口，需复核。")))
    else:
        verdict.append(("中", f"equity.csv 在 {target_hour:02d}点无新行"
                              f"——本轮未产出（截断或未触发/非交易时段）。"))

    printer("\n判定：")
    for level, message in verdict:
        printer(f"  [{level}] {message}")

    return verdict, check_groups(target_hour, now, printer=printer)


def check_groups(target_hour: int, now: dt.datetime, *, printer=print) -> list:
    """其余 12 个账户的本整点产出健康度。

    它们以 --no-jitter 触发、不写 jitter_log，故只能看 equity 是否出现本整点新行。

    ⚠ 账户名单走 `roster`。旧实现在这里硬编码了第 6 份 13 行账户表，
    而且写的是 exp1~exp9 + B/C/D 的**旧名称**（配置改名后就对不上了）。
    """
    printer(f"\n=== 其余各组·本轮产出健康度核对 | 目标整点 {target_hour:02d}:00 ===")
    accounts = [a for a in roster.roster() if not a.is_control]
    lo, hi = GROUP_WINDOW_MIN

    results, produced, missing = [], [], []
    for account in accounts:
        point = latest_equity_in_hour(account.paths.equity, target_hour, now.date())
        if point:
            at, total, ret = point
            delay = delay_minutes(at)
            ok = lo < delay <= hi
            level = "高" if ok else "中"
            message = (f"{account.label}：本轮产出 {at.strftime('%H:%M:%S')}"
                       f"（整点后{delay:.1f}分钟）总资产{float(total or 0):,.0f} 收益{ret}%"
                       + ("，链路正常。" if ok else f"，超出{hi}分钟窗口需复核。"))
            produced.append(account.label)
        else:
            level = "中"
            message = (f"{account.label}：⚠ 本整点无新权益行——本轮未产出"
                       f"（可能被超时截断/未触发/非交易时段空跑）。")
            missing.append(account.label)
        results.append((level, message))

    for level, message in results:
        printer(f"  [{level}] {message}")
    printer(f"\n小结：本整点 {len(produced)}/{len(accounts)} 组已产出"
            + (f"；未产出：{', '.join(missing)}" if missing else "；全部正常。"))
    return results
