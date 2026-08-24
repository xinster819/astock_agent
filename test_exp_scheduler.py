import json
from pathlib import Path

import exp_scheduler


def test_run_all_once_continues_after_one_group_failure(monkeypatch):
    calls = []

    def fake_run(exp_id, force=False, verbose=False):
        calls.append(exp_id)
        if exp_id == "exp2":
            raise RuntimeError("synthetic failure")
        return f"ok:{exp_id}"

    monkeypatch.setattr(exp_scheduler.run_exp, "run_experiment", fake_run)
    result = exp_scheduler.run_all_once(
        ["exp1", "exp2", "exp3"], force=True, retries=0, verbose=False
    )

    assert calls == ["exp1", "exp2", "exp3"]
    assert result["completed"] == ["exp1", "exp3"]
    assert result["failed"][0]["id"] == "exp2"


def test_scheduler_writes_audit_record(tmp_path, monkeypatch):
    monkeypatch.setattr(exp_scheduler.run_exp, "run_experiment", lambda *a, **k: "ok")
    audit = tmp_path / "scheduler.jsonl"
    result = exp_scheduler.run_all_once(["exp1"], force=True, verbose=False, audit_path=audit)

    assert result["completed"] == ["exp1"]
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    assert rows[0]["event"] == "start"
    assert rows[-1]["event"] == "finish"
    assert rows[-1]["completed"] == ["exp1"]


def test_scheduler_retries_failed_group_without_blocking_later_groups(monkeypatch):
    attempts = {}

    def flaky(exp_id, force=False, verbose=False):
        attempts[exp_id] = attempts.get(exp_id, 0) + 1
        if exp_id == "exp2" and attempts[exp_id] == 1:
            raise RuntimeError("temporary")
        return "ok"

    monkeypatch.setattr(exp_scheduler.run_exp, "run_experiment", flaky)
    result = exp_scheduler.run_all_once(["exp1", "exp2", "exp3"], force=True, retries=1, verbose=False)

    assert attempts["exp2"] == 2
    assert result["completed"] == ["exp1", "exp2", "exp3"]
    assert result["failed"] == []
