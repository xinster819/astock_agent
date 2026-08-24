#!/bin/bash
# =============================================================================
# astock_agent 调度心跳 —— 由 launchd/cron 每小时唤醒一次，由本脚本决定是否真跑。
#
# 【为什么是"每小时唤醒 + 脚本自判"，而不是把北京时间换算成本机时间写进 cron】
#   本机时区是 America/Los_Angeles，与北京相差 15/16 小时且随美国夏令时漂移。
#   若把 cron 写成本地时刻，每年 DST 切换两次都会让整套调度错开一小时，
#   而且错开时不会有任何报错 —— 又是一次"静默失效"。
#   现在：唤醒时刻无所谓，脚本用 market_time 取北京时间自行判定，时区漂移免疫。
#
# 【节奏】沿用原系统：每交易日北京时间 10 / 11 / 14 点各一轮；
#         周五收盘后（北京 15 点）跑一次周度数据底座采集。
#
# 用法：
#   ./scheduler_tick.sh          # 正常心跳（由调度器调用）
#   ./scheduler_tick.sh --now    # 忽略时段判断，立刻跑一轮（手动/排障用）
# =============================================================================
set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$BASE/.venv/bin/python"
LOG_DIR="$BASE/logs"
mkdir -p "$LOG_DIR"

if [ ! -x "$PY" ]; then
    echo "🔴 找不到虚拟环境 $PY —— 请先 python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

FORCE_NOW=0
[ "${1:-}" = "--now" ] && FORCE_NOW=1

# ---- 用交易所时钟取北京时间，绝不用本机时区 ----
read -r BJ_DATE BJ_HOUR BJ_DOW <<EOF
$("$PY" -c "import market_time as t; n=t.now(); print(n.strftime('%Y-%m-%d'), int(n.strftime('%H')), n.weekday())" 2>/dev/null)
EOF

if [ -z "${BJ_DATE:-}" ]; then
    echo "🔴 无法取得交易所时间，market_time 不可用" >&2
    exit 1
fi

LOG="$LOG_DIR/tick_${BJ_DATE}.log"
say() { echo "[$(date -u +%H:%M:%SZ) | 北京 ${BJ_DATE} ${BJ_HOUR}时] $*" | tee -a "$LOG"; }

# ---- 时段判定 ----
if [ "$FORCE_NOW" -eq 0 ]; then
    if [ "$BJ_DOW" -ge 5 ]; then
        say "周末休市（北京星期 $((BJ_DOW+1))），跳过。"
        exit 0
    fi
    case "$BJ_HOUR" in
        10|11|14) ;;                       # 交易轮次
        15) # 周五收盘后做周度采集，其余日仅退出
            if [ "$BJ_DOW" -eq 4 ]; then
                say "周五收盘，执行周度数据底座采集。"
                "$PY" -u "$BASE/weekly_collect.py"     >>"$LOG" 2>&1
                "$PY" -u "$BASE/dashboard.py"          >>"$LOG" 2>&1
                "$PY" -u "$BASE/integrity_gate.py"     >>"$LOG" 2>&1
            fi
            exit 0 ;;
        *) exit 0 ;;                        # 非交易时刻，静默退出不写日志
    esac
fi

say "===== 开始一轮 ====="

# ---- A 组（纯规则对照）----
# 抖动收窄到 10~60s：整个 tick 本身已跨数十分钟，原先最多 540s 的抖动
# 对"错开整点峰值"已无边际价值，只是白等。
say "A组 run.py"
JITTER_MIN=10 JITTER_MAX=60 "$PY" -u "$BASE/run.py" >>"$LOG" 2>&1 || say "⚠ A组异常退出 rc=$?"

# ---- exp1~exp9（规则实验组，串行，各自独立账户锁）----
say "exp1~exp9 run_all_exp.py"
"$PY" -u "$BASE/run_all_exp.py" --no-jitter >>"$LOG" 2>&1 || say "⚠ 实验组异常退出 rc=$?"

# ---- B/C/D：由独立的 agent 定时任务整段负责（2026-08-24 恢复）----
# 三段式的中段（agent 决策回合）无法用脚本实现——owner 明确禁止脚本直连 LLM 网关。
# 现由 Claude Code 本地定时任务 astock-agent-bcd-round 承担，它整段跑
# prepare → 写 decision_output.json → execute，节奏为北京 10:35 / 14:35
# （沿用原系统 C/D 组的节奏，且与本心跳的 :05 错开，避免同时打行情源）。
#
# 所以本脚本【不碰】B/C/D，以免两个调度器抢同一批账户。
# 这里仍打印一行，让日志读者一眼看出是"另有归属"而不是又一次静默停摆
# ——2026-07-31 的教训就是"账户不跑而没人发现"。
BCD_HANDLED_ELSEWHERE=1
if [ "$BCD_HANDLED_ELSEWHERE" -eq 1 ]; then
    say "B/C/D 组：由定时 agent 任务 astock-agent-bcd-round 负责（北京 10:35 / 14:35），本心跳不处理。"
else
    for G in B C D; do
        say "${G}组 prepare.py（生成 decision_input.json）"
        ASTOCK_GROUP=$G "$PY" -u "$BASE/prepare.py" --no-jitter >>"$LOG" 2>&1 || say "⚠ ${G}组 prepare 异常 rc=$?"
    done
    for G in B C D; do
        if [ -f "$BASE/group$G/decision_output.json" ]; then
            say "${G}组 execute.py（发现决策文件，落地）"
            ASTOCK_GROUP=$G "$PY" -u "$BASE/execute.py" >>"$LOG" 2>&1 || say "⚠ ${G}组 execute 异常 rc=$?"
        else
            say "${G}组 无 decision_output.json，跳过 execute（中段 agent 未接入）"
        fi
    done
fi

# ---- 每轮收尾：停摆自检。这是 2026-07-31 事故后加的硬性动作 ----
say "停摆自检"
"$PY" - <<'PYEOF' >>"$LOG" 2>&1
import json, os, datetime as dt
import market_time; market_time.enforce()
import freshness_gate as fg

BASE = os.path.dirname(os.path.abspath("__file__")) or "."
ACCTS = [("A组", "state.json")] + \
        [(f"exp{i}", f"experiments/exp{i}_state.json") for i in range(1, 10)] + \
        [(f"{g}组", f"group{g}/state.json") for g in "BCD"]
now = dt.datetime.now()
stalled = []
for name, path in ACCTS:
    if not os.path.exists(path):
        continue
    st = json.load(open(path, encoding="utf-8"))
    r = fg.check(st, [{"时间": now.strftime("%Y-%m-%d %H:%M:%S")}], now=now,
                 review_start=now - dt.timedelta(days=7), review_end=now)
    if any(f["check"] == "stalled_engine" for f in r["red_flags"]):
        stalled.append(name)
print("🔴 停摆账户:", ", ".join(stalled) if stalled else "无")
PYEOF

say "===== 本轮结束 ====="
