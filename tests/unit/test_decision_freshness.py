"""agent 决策文件新鲜度校验的回归测试。

坑的来源（2026-08-23 实测）：groupB/C/D 里躺着 08-20 写的 decision_output.json
——当时时区停摆，execute 从未消费、也就从未归档。而 execute.py 原先拿到文件
就直接执行，对它何时写的毫无判断。一旦进入交易日，就会把三天前的决策按今天的
价格下单，且不会有任何提示。
"""
import io
import json
import os
import tempfile
import unittest


def _touch(path, content, mtime):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False))
    os.utime(path, (mtime, mtime))


class TestDecisionFreshness(unittest.TestCase):

    def setUp(self):
        os.environ.setdefault("ASTOCK_GROUP", "B")
        from astock.pipeline import execute
        self.execute = execute
        self.tmp = tempfile.mkdtemp(prefix="decision_fresh_")
        self.inp = os.path.join(self.tmp, "decision_input.json")
        self.out = os.path.join(self.tmp, "decision_output.json")

    def check(self, raw=None):
        return self.execute.decision_freshness(self.out, self.inp, raw)

    # ---- 拒用的情形 ----

    def test_missing_input_pack_is_refused(self):
        _touch(self.out, {"decisions": []}, 2000)
        ok, why = self.check()
        self.assertFalse(ok)
        self.assertIn("决策包", why)

    def test_output_older_than_input_is_refused(self):
        """事故现场：决策文件比本轮决策包早。"""
        _touch(self.out, {"decisions": [{"action": "buy"}]}, 1000)
        _touch(self.inp, {"ts": "2026-08-23 12:00:00"}, 2000)
        ok, why = self.check()
        self.assertFalse(ok)
        self.assertIn("残留", why)

    def test_same_mtime_is_refused(self):
        """时间戳相同无法证明先后，从严拒用。"""
        _touch(self.out, {"decisions": []}, 1500)
        _touch(self.inp, {"ts": "x"}, 1500)
        self.assertFalse(self.check()[0])

    def test_mismatched_input_ts_is_refused(self):
        """更强的溯源：output 自称属于另一轮决策包。"""
        _touch(self.inp, {"ts": "2026-08-23 12:00:00"}, 1000)
        _touch(self.out, {"decisions": [], "input_ts": "2026-08-20 14:00:00"}, 2000)
        ok, why = self.check({"decisions": [], "input_ts": "2026-08-20 14:00:00"})
        self.assertFalse(ok)
        self.assertIn("input_ts", why)

    # ---- 放行的情形 ----

    def test_fresh_output_passes(self):
        _touch(self.inp, {"ts": "2026-08-23 12:00:00"}, 1000)
        _touch(self.out, {"decisions": [{"action": "buy"}]}, 2000)
        ok, why = self.check()
        self.assertTrue(ok, why)

    def test_matching_input_ts_passes(self):
        _touch(self.inp, {"ts": "2026-08-23 12:00:00"}, 1000)
        _touch(self.out, {"decisions": [], "input_ts": "2026-08-23 12:00:00"}, 2000)
        ok, why = self.check({"decisions": [], "input_ts": "2026-08-23 12:00:00"})
        self.assertTrue(ok, why)

    def test_output_without_input_ts_field_still_passes_on_mtime(self):
        """input_ts 是可选字段，没有就只靠 mtime，不因此拒用。"""
        _touch(self.inp, {"ts": "2026-08-23 12:00:00"}, 1000)
        _touch(self.out, {"decisions": []}, 2000)
        self.assertTrue(self.check({"decisions": []})[0])

    def test_reason_is_never_empty_when_refused(self):
        """拒用必须给出理由——静默跳过正是这次要根治的病。"""
        _touch(self.out, {"decisions": []}, 2000)
        ok, why = self.check()
        self.assertFalse(ok)
        self.assertTrue(why.strip())


class TestStaleFilesAreQuarantined(unittest.TestCase):
    """08-20 的三份残留决策文件应已被改名隔离，不能再被 execute 拾起。"""

    def test_no_live_stale_decision_output_remains(self):
        base = os.path.dirname(os.path.abspath(__file__))
        for g in "BCD":
            live = os.path.join(base, f"group{g}", "decision_output.json")
            if not os.path.exists(live):
                continue
            inp = os.path.join(base, f"group{g}", "decision_input.json")
            if os.path.exists(inp):
                self.assertGreater(
                    os.path.getmtime(live), os.path.getmtime(inp),
                    f"group{g} 存在早于决策包的 decision_output.json，会被当作本轮决策执行")


if __name__ == "__main__":
    unittest.main()
