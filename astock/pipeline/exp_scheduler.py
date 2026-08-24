"""Persistent-friendly supervisor for running all experiment accounts.

The supervisor is intentionally one-shot: invoke it from cron/systemd/task scheduler
on each wake-up.  It does not sleep or open a listening socket.  Each account is
isolated so one failure cannot prevent later accounts from running.
"""
import datetime as dt
import json
from pathlib import Path

from astock.core import experiments as exp_manager
from astock.pipeline import run_exp


def _write_audit(path, row):
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_all_once(exp_ids=None, *, force=False, retries=1, verbose=True, audit_path=None):
    """Run every configured experiment once, retrying failures independently."""
    if exp_ids is None:
        exp_ids = [item["id"] for item in exp_manager.list_experiments()]
    exp_ids = list(exp_ids)
    result = {"started": exp_ids, "completed": [], "failed": []}
    _write_audit(audit_path, {
        "event": "start", "time": dt.datetime.now().isoformat(timespec="seconds"),
        "experiments": exp_ids,
    })

    for exp_id in exp_ids:
        attempts = 0
        last_error = None
        while attempts <= max(0, int(retries)):
            attempts += 1
            try:
                run_exp.run_experiment(exp_id, force=force, verbose=verbose)
                result["completed"].append(exp_id)
                break
            except Exception as exc:  # isolate account failures
                last_error = repr(exc)
        else:
            result["failed"].append({"id": exp_id, "attempts": attempts, "error": last_error})

    _write_audit(audit_path, {
        "event": "finish", "time": dt.datetime.now().isoformat(timespec="seconds"),
        "completed": result["completed"], "failed": result["failed"],
    })
    return result


if __name__ == "__main__":
    from astock.runtime import clock as market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    import sys
    force = "--force" in sys.argv
    result = run_all_once(force=force, audit_path="scheduler_audit.jsonl")
    print(json.dumps(result, ensure_ascii=False, indent=2))
