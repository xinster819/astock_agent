"""astock · 统一命令行入口。

【为什么要有它】
重构前每个功能一个可执行脚本，调度脚本里是这样一串：

    python3 run.py            python3 run_all_exp.py
    ASTOCK_GROUP=B python3 prepare.py     ASTOCK_GROUP=B python3 execute.py
    python3 weekly_collect.py python3 dashboard.py   python3 integrity_gate.py

每个脚本自己解析 sys.argv、自己 `market_time.enforce()`、自己处理抖动，
八份 `if __name__ == "__main__"` 各写各的，参数风格互不相同
（有的用 `--force`，有的用位置参数，有的靠环境变量）。

现在只有一个入口。时区自检、抖动、账户解析这些**每个命令都要做的事**
在这里做一次，子命令只管自己的业务。
"""
from __future__ import annotations

import argparse
import sys

from astock import __version__
from astock.runtime import clock, jitter


def _bootstrap(verify_clock: bool = True) -> None:
    """所有命令的共同前置：把进程时区钉死在交易所时区，钉不住就大声说。

    重构前这两行散在 8 个脚本的入口里，漏掉一个就是一次潜在的时区事故——
    2026-07-31 的三周停摆正是这么来的。
    """
    clock.enforce()
    if verify_clock:
        clock.verify()


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
    """推进一轮：A 组、单个实验组，或全部实验组。"""
    from astock.pipeline import exp_scheduler, run_rule

    jitter.sleep_with_jitter(enabled=not args.no_jitter and not args.force)

    target = args.account.lower()
    if target in ("all", "exp", "all-exp"):
        result = exp_scheduler.run_all_once(force=args.force, verbose=not args.quiet)
        print(f"\n完成 {len(result['completed'])}/{len(result['started'])} 个实验组")
        for failure in result["failed"]:
            print(f"  ✗ {failure['id']}: {failure['error']}")
        return 1 if result["failed"] else 0

    if target.startswith("exp"):
        report = run_rule.run_experiment(target, force=args.force, verbose=not args.quiet)
        return 0 if report else 1

    # 被冷却/锁跳过是**正常**行为，不是故障——返回非零会让调度脚本误报警。
    run_rule.run_control(force=args.force, verbose=not args.quiet)
    return 0


def cmd_prepare(args) -> int:
    """生成 agent 决策包（三段式流水第一段）。"""
    from astock.pipeline import prepare

    jitter.sleep_with_jitter(enabled=not args.no_jitter)
    pack = prepare.build(args.group)
    account = pack["account"]
    print(f"决策包已生成: group{pack['group']}/decision_input.json")
    print(f"  市场: {pack['market_status']} | 总资产 {account['total_asset']:,.2f}"
          f" | 现金 {account['cash']:,.2f} | 持仓 {len(pack['positions'])} 只")
    print(f"  行情异常 {len(pack['quote_quality']['bad'])} 只"
          f" | 单源降级 {len(pack['quote_quality']['single_source'])} 只")
    print(f"  规则候选 {len(pack['rule_candidates'])} 条")
    return 0


def cmd_execute(args) -> int:
    """落地 agent 决策（三段式流水第三段）。"""
    from astock.pipeline import execute

    execute.execute(args.group, force=args.force, verbose=not args.quiet)
    return 0


def cmd_report(args) -> int:
    from astock.reporting import report

    use_live = not args.offline
    print(report.account_report(args.account, use_live=use_live) if args.account
          else report.summary_table(use_live=use_live))
    return 0


def cmd_check(args) -> int:
    """账本完整性体检：现金单调性、重复下单、trades 重放对账、负现金。"""
    from astock.guards import integrity

    dirty = integrity.run_cli()
    if dirty:
        print(f"\n🔴 {dirty} 个账户账实不符。脏账户不得进入收益排名与归因；"
              f"确认成因后可用 `astock clean-ghosts` 预演清洗。")
    return 0        # 体检是报告，不是门禁——非零会让调度脚本整体报警


def cmd_dashboard(args) -> int:
    """生成对照实验观察台（单文件 HTML，双击即开，不起任何服务）。"""
    from astock.reporting import console

    # 带实时行情时要跑几分钟（13 个账户的持仓逐只三源交叉验证 + 基准指数），
    # 静默几分钟会让人以为挂了。
    step = 0

    def progress(message: str) -> None:
        nonlocal step
        step += 1
        print(f"  [{step}/5] {message}", flush=True)

    payload = console.build(use_live=not args.offline, progress=progress)
    path = console.render(payload)

    verdict = payload["verdict"]
    print(f"观察台已生成: {path}")
    print(f"  {verdict['headline']}")
    for reason in verdict["reasons"]:
        print(f"    — {reason}")
    health = payload["health"]
    if not health["all_clear"]:
        print(f"  🔴 {health['note']}")
    return 0


def cmd_weekly(args) -> int:
    """周度复盘数据底座。"""
    from astock.reporting import weekly

    weekly.main(week_str=args.week, use_live=not args.offline)
    return 0


def cmd_stall_check(args) -> int:
    """引擎停摆自检：有账户在跑却从未进入下单分支吗？"""
    from astock.ops import stall_check

    stall_check.report()
    return 0        # 报告即目的，不因发现停摆而让调度脚本整体失败


def cmd_check_jitter(args) -> int:
    """抖动与截断核对：本整点各账户是否真的跑了、有没有被超时杀掉。"""
    from astock.ops import check_jitter

    check_jitter.check(args.hour)
    return 0


def cmd_clean_ghosts(args) -> int:
    """清洗历史遗留的并发幽灵成交。默认预演，加 --apply 才写盘。"""
    from astock.ops import clean_ghost_trades

    results = clean_ghost_trades.clean_all(dry_run=not args.apply)
    changed = [r.account for r in results if r.changed]
    if not args.apply:
        print("\n预演模式，未写盘。确认无误后加 --apply 执行。")
    elif changed:
        print(f"\n已清洗 {len(changed)} 个账户：{', '.join(changed)}（原文件均已备份）")
    return 0


def cmd_doctor(args) -> int:
    """环境体检：时钟、工作区、配置、13 个账户的账本是否就位。"""
    from astock.runtime import paths

    print("== astock 环境体检 ==")
    ok = clock.offset_ok()
    print(f"  交易所时钟   : {clock.stamp()}  进程时区达标={ok}")
    print(f"  工作区       : {paths.workspace()}")
    print(f"  配置根       : {paths.config_root()}")
    missing = [a.account for a in paths.all_accounts() if not a.state.exists()]
    print(f"  账户账本     : {13 - len(missing)}/13 已初始化"
          + (f"（未初始化: {', '.join(missing)}）" if missing else ""))
    if not ok:
        print("  🔴 进程时区不是 UTC+8，账本日期标签会整体错位——请设置 TZ=Asia/Shanghai")
    return 0 if ok else 1


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astock", description="A 股多策略模拟交易系统")
    parser.add_argument("--version", action="version", version=f"astock {__version__}")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出结果，不打印轮次明细")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="推进一轮交易")
    run.add_argument("account", nargs="?", default="A",
                     help="A（默认）、exp1~exp9，或 all 跑全部实验组")
    run.add_argument("--force", action="store_true",
                     help="跳过交易时段判断强制成交（其余硬校验照旧）")
    run.add_argument("--no-jitter", action="store_true", help="关闭随机延时")
    run.set_defaults(func=cmd_run)

    prepare = sub.add_parser("prepare", help="生成 agent 决策包")
    prepare.add_argument("group", nargs="?", default=None, help="B / C / D")
    prepare.add_argument("--no-jitter", action="store_true")
    prepare.set_defaults(func=cmd_prepare)

    execute = sub.add_parser("execute", help="落地 agent 决策")
    execute.add_argument("group", nargs="?", default=None, help="B / C / D")
    execute.add_argument("--force", action="store_true")
    execute.set_defaults(func=cmd_execute)

    report = sub.add_parser("report", help="账户报告；省略账户则输出 13 账户汇总表")
    report.add_argument("account", nargs="?", default=None)
    report.add_argument("--offline", action="store_true", help="不拉实时行情，按成本估值")
    report.set_defaults(func=cmd_report)

    check = sub.add_parser("check", help="账本完整性体检")
    check.set_defaults(func=cmd_check)

    dashboard = sub.add_parser("dashboard", help="生成对照实验观察台（单文件 HTML）")
    dashboard.add_argument("--offline", action="store_true", help="不拉实时行情")
    dashboard.set_defaults(func=cmd_dashboard)

    weekly = sub.add_parser("weekly", help="周度复盘数据采集")
    weekly.add_argument("--week", default=None, help="指定 ISO 周，如 2026-W34；省略取本周")
    weekly.add_argument("--offline", action="store_true", help="不拉实时指数")
    weekly.set_defaults(func=cmd_weekly)

    jitter_cmd = sub.add_parser("check-jitter", help="抖动与超时截断核对")
    jitter_cmd.add_argument("hour", nargs="?", type=int, default=None,
                            help="目标整点 0-23，省略取当前小时")
    jitter_cmd.set_defaults(func=cmd_check_jitter)

    clean = sub.add_parser("clean-ghosts", help="清洗历史遗留的并发幽灵成交")
    # 默认预演：这个命令会重写 trades.csv，改账本必须是显式动作
    clean.add_argument("--apply", action="store_true", help="真正写盘（默认只预演）")
    clean.set_defaults(func=cmd_clean_ghosts)

    stall = sub.add_parser("stall-check", help="引擎停摆自检（每轮收尾跑）")
    stall.set_defaults(func=cmd_stall_check)

    doctor = sub.add_parser("doctor", help="环境体检：时钟 / 工作区 / 账本")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _bootstrap()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
