"""runtime.jitter · 调度抖动与它的截断检测日志。

抖动本身很简单，值得测的是它的**日志契约**：睡前落一行"计划"、睡醒补一行
"已开跑"。只有计划行、没有开跑行，就说明进程在睡眠中被调度器超时杀掉了。

没有这对行，截断只会表现为"那一轮什么都没发生"——又一次静默失效。
"""
import pytest

from astock.core.ledger import read_csv_rows
from astock.runtime import jitter, paths


@pytest.fixture
def no_sleep(monkeypatch):
    """记录 sleep 时长但不真的睡。"""
    slept = []
    monkeypatch.setattr(jitter.time, "sleep", slept.append)
    return slept


class TestBounds:

    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("JITTER_MIN", raising=False)
        monkeypatch.delenv("JITTER_MAX", raising=False)
        assert jitter.bounds() == (jitter.DEFAULT_MIN_SEC, jitter.DEFAULT_MAX_SEC)

    def test_env_override(self, monkeypatch):
        """A 组用更短的窗口错峰，靠的就是这两个环境变量。"""
        monkeypatch.setenv("JITTER_MIN", "10")
        monkeypatch.setenv("JITTER_MAX", "60")
        assert jitter.bounds() == (10, 60)

    def test_inverted_bounds_are_swapped_not_crashed(self, monkeypatch):
        """min>max 是配置写反了，交换即可——绝不要因此让整轮交易崩掉。"""
        monkeypatch.setenv("JITTER_MIN", "300")
        monkeypatch.setenv("JITTER_MAX", "30")
        assert jitter.bounds() == (30, 300)


class TestSleepWithJitter:

    def test_disabled_returns_immediately(self, isolated_env, no_sleep):
        assert jitter.sleep_with_jitter(enabled=False, printer=lambda *_: None) == 0
        assert no_sleep == []
        assert not paths.jitter_log().exists(), "没抖动就不该留日志"

    def test_sleeps_within_bounds(self, isolated_env, no_sleep, monkeypatch):
        monkeypatch.setenv("JITTER_MIN", "60")
        monkeypatch.setenv("JITTER_MAX", "90")
        waited = jitter.sleep_with_jitter(printer=lambda *_: None)
        assert 60 <= waited <= 90
        assert no_sleep == [waited]

    def test_writes_the_planned_and_fired_pair(self, isolated_env, no_sleep):
        """两行日志是截断检测的全部依据。"""
        jitter.sleep_with_jitter(printer=lambda *_: None)
        rows = read_csv_rows(paths.jitter_log())
        assert [r["状态"] for r in rows] == ["sleeping", "fired"]
        assert rows[0]["实际开跑时刻"] == "", "计划行还不知道实际开跑时刻"
        assert rows[1]["实际开跑时刻"] != ""
        assert rows[0]["唤醒时刻"] == rows[1]["唤醒时刻"], "两行须能配对"

    def test_planned_row_survives_alone_when_killed_midsleep(self, isolated_env, monkeypatch):
        """模拟睡眠中被 SIGKILL：只留下计划行，没有开跑行——这正是要能看出来的。"""
        def killed(_seconds):
            raise KeyboardInterrupt("模拟调度器超时杀进程")

        monkeypatch.setattr(jitter.time, "sleep", killed)
        with pytest.raises(KeyboardInterrupt):
            jitter.sleep_with_jitter(printer=lambda *_: None)

        rows = read_csv_rows(paths.jitter_log())
        assert [r["状态"] for r in rows] == ["sleeping"]

    def test_log_lands_in_the_workspace(self, isolated_env, no_sleep):
        jitter.sleep_with_jitter(printer=lambda *_: None)
        assert paths.jitter_log().parent == isolated_env
