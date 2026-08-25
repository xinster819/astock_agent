"""agent 决策文件新鲜度校验。

坑的来源（2026-08-23 实测）：groupB/C/D 里躺着 08-20 写的 decision_output.json
——当时时区停摆，execute 从未消费、也就从未归档。而 execute 原先拿到文件就直接
执行，对它何时写的毫无判断。一旦进入交易日，就会把三天前的决策按今天的价格下单，
且不会有任何提示。

被测契约：`execute.decision_freshness(paths, raw=None) -> (ok, reason)`
  拒用的三条判据：缺本轮决策包 / output 不晚于 input / input_ts 对不上。
  拒用时 reason 必须非空——静默跳过正是这次要根治的病。
"""
import json
import os

import pytest

from astock.pipeline.execute import decision_freshness
from astock.runtime.paths import AccountPaths


def _write(path, payload, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def group_paths(isolated_env):
    """一个干净的 B 组目录（决策文件尚不存在）。"""
    return AccountPaths.for_group("B").ensure_dirs()


# ---------------------------------------------------------------- 拒用的情形

def test_missing_input_pack_is_refused(group_paths):
    """没有本轮决策包 = 无从确认决策依据。"""
    _write(group_paths.decision_output, {"decisions": []}, 2000)
    ok, why = decision_freshness(group_paths)
    assert not ok
    assert "决策包" in why


def test_output_older_than_input_is_refused(group_paths):
    """事故现场：决策文件比本轮决策包还早，是上一轮的残留。"""
    _write(group_paths.decision_output, {"decisions": [{"action": "buy"}]}, 1000)
    _write(group_paths.decision_input, {"ts": "2026-08-23 12:00:00"}, 2000)
    ok, why = decision_freshness(group_paths)
    assert not ok
    assert "残留" in why


def test_same_mtime_is_refused(group_paths):
    """时间戳相同无法证明先后，从严拒用。"""
    _write(group_paths.decision_output, {"decisions": []}, 1500)
    _write(group_paths.decision_input, {"ts": "x"}, 1500)
    assert decision_freshness(group_paths)[0] is False


def test_mismatched_input_ts_is_refused(group_paths):
    """比 mtime 更强的溯源：output 自称属于另一轮决策包。"""
    _write(group_paths.decision_input, {"ts": "2026-08-23 12:00:00"}, 1000)
    raw = {"decisions": [], "input_ts": "2026-08-20 14:00:00"}
    _write(group_paths.decision_output, raw, 2000)
    ok, why = decision_freshness(group_paths, raw=raw)
    assert not ok
    assert "input_ts" in why


def test_refusal_always_carries_a_reason(group_paths):
    """拒用必须给出理由——宁可吵，也不沉默。"""
    _write(group_paths.decision_output, {"decisions": []}, 2000)
    ok, why = decision_freshness(group_paths)
    assert not ok
    assert why.strip()


# ---------------------------------------------------------------- 放行的情形

def test_fresh_output_passes(group_paths):
    _write(group_paths.decision_input, {"ts": "2026-08-23 12:00:00"}, 1000)
    _write(group_paths.decision_output, {"decisions": [{"action": "buy"}]}, 2000)
    ok, why = decision_freshness(group_paths)
    assert ok, why


def test_matching_input_ts_passes(group_paths):
    _write(group_paths.decision_input, {"ts": "2026-08-23 12:00:00"}, 1000)
    raw = {"decisions": [], "input_ts": "2026-08-23 12:00:00"}
    _write(group_paths.decision_output, raw, 2000)
    ok, why = decision_freshness(group_paths, raw=raw)
    assert ok, why


def test_absent_input_ts_field_falls_back_to_mtime(group_paths):
    """input_ts 是可选字段：没带就只靠 mtime 判定，不因缺字段而拒用。"""
    _write(group_paths.decision_input, {"ts": "2026-08-23 12:00:00"}, 1000)
    _write(group_paths.decision_output, {"decisions": []}, 2000)
    assert decision_freshness(group_paths, raw={"decisions": []})[0] is True
