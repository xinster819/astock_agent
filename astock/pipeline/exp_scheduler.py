"""exp_scheduler · 批量推进 exp1~exp9 各一轮。

刻意做成**一次性**的：由 cron/launchd 每次唤醒时调用一次，自己不睡眠、
不开监听端口（这是项目的硬约束之一）。

【合并说明】
重构前"跑一遍所有实验组"有两份实现：
  `run_all_exp.py`   有全局抖动和汇总打印，没有重试、没有审计
  `exp_scheduler.py` 有独立重试和审计日志，没有抖动
两者被不同的入口调用，行为因此不一致。现在合并为这一份，两边的能力都保留。

账户之间严格隔离：任何一个账户抛异常都不会阻断后续账户——
13 个账户互为对照，让一个坏掉的账户拖垮整批会直接毁掉当天的对照数据。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from astock.core import experiments
from astock.pipeline import run_rule
from astock.runtime import jitter

#: 实验组之间的间隔，避免对行情站点形成瞬时并发
INTER_ACCOUNT_PAUSE_SEC = 2


def _write_audit(path, row: dict[str, Any]) -> None:
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_all_once(
    exp_ids: Iterable[str] | None = None,
    *,
    force: bool = False,
    retries: int = 1,
    verbose: bool = True,
    audit_path: str | Path | None = None,
    use_jitter: bool = False,
    pause_sec: float = INTER_ACCOUNT_PAUSE_SEC,
) -> dict[str, Any]:
    """每个实验组各跑一轮，失败独立重试，返回 {started, completed, failed}。"""
    if exp_ids is None:
        exp_ids = [item["id"] for item in experiments.list_experiments()]
    exp_ids = list(exp_ids)
    result: dict[str, Any] = {"started": exp_ids, "completed": [], "failed": []}

    _write_audit(audit_path, {
        "event": "start", "time": dt.datetime.now().isoformat(timespec="seconds"),
        "experiments": exp_ids,
    })

    # 全局只抖一次：13 个账户各抖一次会把整批拖过调度器的 10 分钟上限
    jitter.sleep_with_jitter(enabled=use_jitter and not force,
                             printer=print if verbose else lambda *_: None)

    for index, exp_id in enumerate(exp_ids):
        attempts, last_error = 0, None
        while attempts <= max(0, int(retries)):
            attempts += 1
            try:
                run_rule.run_experiment(exp_id, force=force, verbose=verbose)
                result["completed"].append(exp_id)
                break
            except Exception as exc:      # 隔离单账户故障，绝不中断整批
                last_error = repr(exc)
                if verbose:
                    print(f"  ✗ {exp_id} 第 {attempts} 次尝试失败：{last_error}")
        else:
            result["failed"].append({"id": exp_id, "attempts": attempts, "error": last_error})

        if pause_sec and index < len(exp_ids) - 1:
            time.sleep(pause_sec)

    _write_audit(audit_path, {
        "event": "finish", "time": dt.datetime.now().isoformat(timespec="seconds"),
        "completed": result["completed"], "failed": result["failed"],
    })
    return result
