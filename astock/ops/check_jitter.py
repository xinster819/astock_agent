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
import os
import sys
import datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
JLOG = os.path.join(BASE, "jitter_log.csv")
EQ = os.path.join(BASE, "equity.csv")
SP = os.path.join(BASE, "spread_log.csv")

# 其余各组的权益账本（用于产出健康度核对）。格式与 A组一致：
#   时间,现金,持仓市值,总资产,累计收益率%
# 说明：这些组由 run_exp.py / execute.py 以 --no-jitter 触发，不写 jitter_log，
# 故只核对"本整点是否产出新行"，不核对抖动。
OTHER_GROUPS = [
    ("exp1·基准策略", os.path.join(BASE, "experiments", "exp1_equity.csv")),
    ("exp2·放宽买入", os.path.join(BASE, "experiments", "exp2_equity.csv")),
    ("exp3·严格趋势", os.path.join(BASE, "experiments", "exp3_equity.csv")),
    ("exp4·金叉策略", os.path.join(BASE, "experiments", "exp4_equity.csv")),
    ("exp5·纯动量", os.path.join(BASE, "experiments", "exp5_equity.csv")),
    ("exp6·状态适配趋势", os.path.join(BASE, "experiments", "exp6_equity.csv")),
    ("exp7·均值回归", os.path.join(BASE, "experiments", "exp7_equity.csv")),
    ("exp8·质量突破", os.path.join(BASE, "experiments", "exp8_equity.csv")),
    ("exp9·多因子排序", os.path.join(BASE, "experiments", "exp9_equity.csv")),
    ("B组·Agent决策", os.path.join(BASE, "groupB", "equity.csv")),
    ("C组·多空辩论", os.path.join(BASE, "groupC", "equity.csv")),
    ("D组·新闻情绪", os.path.join(BASE, "groupD", "equity.csv")),
]


def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    return [l.split(",") for l in lines[1:]] if len(lines) > 1 else []


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


def check(target_hour=None):
    now = dt.datetime.now()
    if target_hour is None:
        target_hour = now.hour
    print(f"=== A组·抖动+截断深度核对 | 目标整点 {target_hour:02d}:00 | 现在 {now.strftime('%H:%M:%S')} ===")

    # ---- 1) jitter_log：按'唤醒时刻'配对 sleeping/fired，找该整点窗口内的孤儿 ----
    jrows = _rows(JLOG)
    by_wake = {}   # 唤醒时刻 -> {'sleeping':row, 'fired':row}
    for r in jrows:
        if len(r) < 5:
            continue
        wake = _parse_hms(r[0])
        if not wake or wake.hour != target_hour:
            continue
        by_wake.setdefault(r[0], {})[r[4]] = r

    verdict = []
    if not jrows:
        verdict.append(("低", "jitter_log.csv 不存在或为空——可能本轮用了 --no-jitter，或尚未触发。"))
    elif not by_wake:
        verdict.append(("中", f"{target_hour:02d}点窗口内 jitter_log 无记录——该整点可能未触发。"))
    else:
        # 取该整点最后一次唤醒事件来判定（最新一轮）
        last_wake = sorted(by_wake.keys())[-1]
        ev = by_wake[last_wake]
        orphans = [w for w, e in by_wake.items() if "sleeping" in e and "fired" not in e]
        if "fired" in ev:
            f = ev["fired"]
            plan, actual = float(f[1]), float(f[3])
            diff = abs(plan - actual)
            if diff <= 3:
                verdict.append(("高", f"抖动真实生效：计划{plan:.0f}s vs 实际{actual:.0f}s（差{diff:.1f}s≤3s）"
                                      f"，{f[0]}唤醒→{f[2]}开跑。"))
            else:
                verdict.append(("中", f"已开跑但延时偏差大：计划{plan:.0f}s vs 实际{actual:.0f}s（差{diff:.1f}s），"
                                      "可能调度器睡眠期间有挂起/暂停。"))
        else:
            # 最新一轮只有 sleeping 无 fired = 截断
            s = ev["sleeping"]
            verdict.append(("高", f"⚠ 检测到超时截断：{s[0]}进入{float(s[1]):.0f}s睡眠后无 fired 行——"
                                  "进程在睡眠中被杀（Bash timeout 太短）。需调大执行端 timeout。"))
        if orphans:
            verdict.append(("中", f"另发现 {len(orphans)} 个孤儿睡眠记录（唤醒于 {', '.join(orphans)}）"
                                  "——历史上有过被截断的轮次，建议复核。"))

    # ---- 2) equity / spread：该整点是否有新产物 ----
    def latest_in_hour(rows, idx=0):
        best = None
        for r in rows:
            t = _hour_of(r[idx])
            # 必须比完整日期。旧实现只比 t.day（几号），每月同一日号会把上个月
            # 的行当成"本轮产物"报「链路正常」—— 恰恰在停摆故障上发虚假绿灯。
            if t and t.hour == target_hour and t.date() == now.date():
                if not best or t > best:
                    best = t
        return best

    eq_t = latest_in_hour(_rows(EQ))
    sp_t = latest_in_hour(_rows(SP))
    if eq_t:
        delay_min = (eq_t - eq_t.replace(minute=0, second=0)).total_seconds() / 60
        ok = 0 < delay_min <= 10
        verdict.append(("高" if ok else "中",
                        f"equity.csv 本轮产物 {eq_t.strftime('%H:%M:%S')}（整点后{delay_min:.1f}分钟）"
                        f"{'，落在1~10分钟合理窗口，未被截断。' if ok else '，超出预期窗口，需复核。'}"))
    else:
        verdict.append(("中", f"equity.csv 在 {target_hour:02d}点无新行——本轮未产出（截断或未触发/非交易时段）。"))
    if sp_t:
        verdict.append(("高", f"spread_log.csv 本轮采样 {sp_t.strftime('%H:%M:%S')}，价差采样已执行。"))

    # ---- 3) 自洽性：fired 时刻应早于 equity 时间戳约 20s ----
    last_fired = None
    if by_wake:
        ev = by_wake[sorted(by_wake.keys())[-1]]
        last_fired = ev.get("fired")
    if last_fired and eq_t:
        ft = _parse_hms(last_fired[2])
        if ft:
            gap = (eq_t - eq_t.replace(hour=ft.hour, minute=ft.minute, second=ft.second)).total_seconds()
            verdict.append(("高" if 0 < gap < 120 else "中",
                            f"自洽性：开跑→产出耗时 {gap:.0f}s"
                            f"{'（≈实跑耗时，链路完整）。' if 0 < gap < 120 else '（异常，需查）。'}"))

    print("\n判定：")
    for lvl, msg in verdict:
        print(f"  [{lvl}] {msg}")

    # ---- B) 其余各组产出健康度核对 ----
    group_verdict = check_groups(target_hour, now)
    return verdict, group_verdict


def _latest_row_in_hour(path, target_hour, day):
    """返回该 csv 在指定整点当天的最后一行(时间戳, 总资产, 收益率)，无则 None。

    day 必须是 datetime.date（完整日期）。旧实现传的是 now.day（几号），
    只比日号不比年月，会把一个月前的旧行误判为本轮产出。
    """
    if not os.path.exists(path):
        return None, "缺文件"
    with open(path, encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    if len(lines) <= 1:
        return None, "空账本"
    best = None
    for line in lines[1:]:
        cols = line.split(",")
        if len(cols) < 5:
            continue
        t = _hour_of(cols[0])
        if t and t.hour == target_hour and t.date() == day:
            if not best or t > best[0]:
                best = (t, cols[3], cols[4])
    return best, None


def check_groups(target_hour, now):
    """对 exp1~8 + B组做本整点产出健康度核对（无 jitter_log，只看 equity 新行）。"""
    print(f"\n=== 实验组+B组·本轮产出健康度核对 | 目标整点 {target_hour:02d}:00 ===")
    results = []
    produced, missing = [], []
    for name, path in OTHER_GROUPS:
        row, err = _latest_row_in_hour(path, target_hour, now.date())
        if row:
            t, total, ret = row
            delay_min = (t - t.replace(minute=0, second=0)).total_seconds() / 60
            # 实验组整点+5分触发、B组+10分触发，且实验组5组串行累计耗时，
            # 故产出落在整点后 5~20 分钟均属正常（区别于A组整点触发的1~10分钟窗口）。
            ok = 0 < delay_min <= 20
            lvl = "高" if ok else "中"
            msg = (f"{name}：本轮产出 {t.strftime('%H:%M:%S')}（整点后{delay_min:.1f}分钟）"
                   f"总资产{float(total):,.0f} 收益{ret}%"
                   f"{'，链路正常。' if ok else '，超出20分钟窗口需复核。'}")
            produced.append(name)
        else:
            lvl = "中"
            reason = err or "本整点无新权益行"
            msg = (f"{name}：⚠ {reason}——本轮未产出"
                   "（可能被超时截断/未触发/非交易时段空跑）。")
            missing.append(name)
        results.append((lvl, msg))

    for lvl, msg in results:
        print(f"  [{lvl}] {msg}")

    # 汇总一行，便于一眼判断
    print(f"\n小结：本整点 {len(produced)}/{len(OTHER_GROUPS)} 组已产出"
          + (f"；未产出：{', '.join(missing)}" if missing else "；全部正常。"))
    return results


if __name__ == "__main__":
    from astock.runtime import clock as market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    h = int(sys.argv[1]) if len(sys.argv) > 1 else None
    check(h)
