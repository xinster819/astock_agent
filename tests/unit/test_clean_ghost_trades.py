"""ops.clean_ghost_trades · 幽灵成交清洗。

这个工具会**重写 trades.csv**——账本是这个系统里唯一不可恢复的东西，
而它此前覆盖率是 0。本文件把它的三条安全承诺逐条钉死：

  1. 只删确定性判据命中的行（窗口内同票同向重复，删较早那一行）
  2. 清完自检不过就**回滚不写盘**
  3. 写盘前必须先落备份

重构中修掉的两个问题也在这里回归：账户名单曾只覆盖 6/13（exp6~exp9 与 C/D 组的
幽灵成交永远清不到），以及判重的行序盲区。
"""

import pytest

from astock.core.ledger import TRADE_COLUMNS
from astock.ops import clean_ghost_trades as cleaner
from astock.runtime import files
from astock.runtime.paths import AccountPaths


def row(ts, side="买入", code="600519", qty=1000, cash=989_994.90, note=""):
    return [ts, side, code, "测试股", 10.0, qty, qty * 10.0, 5.10, cash, note]


def _write(account_paths, rows, state):
    account_paths.ensure_dirs()
    files.write_json_atomic(account_paths.state, state)
    for r in rows:
        files.append_csv_row(account_paths.trades, TRADE_COLUMNS, r)


@pytest.fixture
def exp1(isolated_env):
    return AccountPaths.for_experiment("exp1")


# =========================================================== 判据

class TestGhostRowIndices:

    def test_drops_the_earlier_of_a_rapid_pair(self):
        """后写覆盖先写，所以 state 记的是较晚那一笔——保留晚的、删早的。"""
        rows = [{"时间": "2026-07-09 14:45:20", "代码": "002415", "方向": "买入"},
                {"时间": "2026-07-09 14:45:26", "代码": "002415", "方向": "买入"}]
        assert cleaner.ghost_row_indices(rows) == {0}

    def test_ignores_pairs_outside_the_window(self):
        rows = [{"时间": "2026-07-09 10:00:00", "代码": "002415", "方向": "买入"},
                {"时间": "2026-07-09 14:00:00", "代码": "002415", "方向": "买入"}]
        assert cleaner.ghost_row_indices(rows) == set()

    def test_different_codes_are_not_duplicates(self):
        rows = [{"时间": "2026-07-09 14:45:20", "代码": "002415", "方向": "买入"},
                {"时间": "2026-07-09 14:45:22", "代码": "600519", "方向": "买入"}]
        assert cleaner.ghost_row_indices(rows) == set()

    def test_opposite_sides_are_not_duplicates(self):
        """同一秒买入又卖出是可疑，但不是「重复下单」——判据必须精确。"""
        rows = [{"时间": "2026-07-09 14:45:20", "代码": "002415", "方向": "买入"},
                {"时间": "2026-07-09 14:45:22", "代码": "002415", "方向": "卖出"}]
        assert cleaner.ghost_row_indices(rows) == set()

    def test_reversed_timestamps_are_still_caught(self):
        """行序盲区回归：第二行时间戳更早时也必须判为重复。

        账本历史上时间列记的是取价时刻而非成交时刻，倒序真实存在。
        只判 `0 <= gap` 会漏掉这一半。
        """
        rows = [{"时间": "2026-07-09 14:45:26", "代码": "002415", "方向": "买入"},
                {"时间": "2026-07-09 14:45:20", "代码": "002415", "方向": "买入"}]
        assert cleaner.ghost_row_indices(rows) == {0}

    def test_unparseable_time_rows_are_skipped(self):
        rows = [{"时间": "坏数据", "代码": "002415", "方向": "买入"}]
        assert cleaner.ghost_row_indices(rows) == set()


class TestReplayPositions:

    def test_nets_buys_against_sells(self):
        rows = [{"代码": "600519", "方向": "买入", "数量": "1000"},
                {"代码": "600519", "方向": "卖出", "数量": "400"}]
        assert cleaner.replay_positions(rows) == {"600519": 600}

    def test_fully_closed_positions_disappear(self):
        rows = [{"代码": "600519", "方向": "买入", "数量": "1000"},
                {"代码": "600519", "方向": "卖出", "数量": "1000"}]
        assert cleaner.replay_positions(rows) == {}


# =========================================================== 清洗行为

class TestCleanAccount:

    def test_clean_ledger_is_left_untouched(self, exp1):
        """幂等：已经干净的账户直接跳过，一个字节都不动。"""
        _write(exp1, [row("2026-07-09 10:00:00")],
               {"cash": 989_994.90, "init_cash": 1_000_000.0,
                "positions": {"600519": {"qty": 1000, "available": 0, "cost": 10.005}}})
        before = exp1.trades.read_bytes()
        result = cleaner.clean_account(exp1)
        assert not result.changed
        assert "本就 clean" in result.message
        assert exp1.trades.read_bytes() == before

    def test_missing_account_is_skipped(self, exp1):
        assert "文件缺失" in cleaner.clean_account(exp1).message

    def test_removes_the_ghost_row_and_writes_back(self, exp1):
        """现场重建：两进程各下一单，state 只记下后写的那笔。"""
        _write(exp1,
               [row("2026-07-09 14:45:20", cash=989_994.90),
                row("2026-07-09 14:45:26", cash=989_994.90)],   # 幽灵行
               {"cash": 989_994.90, "init_cash": 1_000_000.0,
                "positions": {"600519": {"qty": 1000, "available": 0, "cost": 10.005}}})

        result = cleaner.clean_account(exp1)
        assert result.changed
        assert result.dropped == [0]
        assert len(files.read_csv_rows(exp1.trades)) == 1

    def test_backup_is_written_before_the_rewrite(self, exp1):
        """备份是回滚的唯一凭据，必须真的落在磁盘上。"""
        original = [row("2026-07-09 14:45:20"), row("2026-07-09 14:45:26")]
        _write(exp1, original,
               {"cash": 989_994.90, "init_cash": 1_000_000.0,
                "positions": {"600519": {"qty": 1000, "available": 0, "cost": 10.005}}})
        before = exp1.trades.read_text(encoding="utf-8")

        result = cleaner.clean_account(exp1)
        assert result.backup and result.backup.exists()
        assert result.backup.read_text(encoding="utf-8") == before

    def test_dry_run_never_touches_the_disk(self, exp1):
        _write(exp1,
               [row("2026-07-09 14:45:20"), row("2026-07-09 14:45:26")],
               {"cash": 989_994.90, "init_cash": 1_000_000.0,
                "positions": {"600519": {"qty": 1000, "available": 0, "cost": 10.005}}})
        before = exp1.trades.read_bytes()

        result = cleaner.clean_account(exp1, dry_run=True)
        assert not result.changed
        assert "dry-run" in result.message
        assert exp1.trades.read_bytes() == before
        assert not list(exp1.root.glob("*.bak.*"))

    def test_rolls_back_when_the_result_would_not_reconcile(self, exp1):
        """删完对不上账就必须回滚——宁可留着脏账，也不能改出一本新的错账。"""
        _write(exp1,
               [row("2026-07-09 14:45:20"), row("2026-07-09 14:45:26")],
               # state 声称持有 2000 股，删掉一行后重放只剩 1000
               {"cash": 989_994.90, "init_cash": 1_000_000.0,
                "positions": {"600519": {"qty": 2000, "available": 0, "cost": 10.005}}})
        before = exp1.trades.read_bytes()

        result = cleaner.clean_account(exp1)
        assert not result.changed
        assert "回滚不写盘" in result.message
        assert exp1.trades.read_bytes() == before

    def test_dirty_for_another_reason_is_reported_not_guessed(self, exp1):
        """脏但不是重复下单造成的，就明说，不要瞎删行。"""
        _write(exp1, [row("2026-07-09 10:00:00")],
               {"cash": 1.0, "init_cash": 1_000_000.0, "positions": {}})
        result = cleaner.clean_account(exp1)
        assert not result.changed
        assert "未定位到窗口内重复行" in result.message


class TestCleanAll:

    def test_covers_all_thirteen_accounts(self, isolated_env):
        """回归：账户名单曾只列了 A / exp1~exp5 / B，

        exp6~exp9 与 C/D 组的幽灵成交因此永远清不到。
        """
        lines = []
        results = cleaner.clean_all(dry_run=True, printer=lines.append)
        assert len(results) == 13
        covered = {r.account for r in results}
        assert {"exp6", "exp7", "exp8", "exp9", "C", "D"} <= covered

    def test_reverifies_every_account_afterwards(self, exp1):
        """清洗后复检必须真的跑。

        它原先是 `os.system("python3 .../integrity_gate.py")`——分包后那个文件
        已不存在，复检静默什么也没做。
        """
        _write(exp1, [row("2026-07-09 10:00:00")],
               {"cash": 989_994.90, "init_cash": 1_000_000.0,
                "positions": {"600519": {"qty": 1000, "available": 0, "cost": 10.005}}})
        lines = []
        cleaner.clean_all(dry_run=True, printer=lines.append)
        text = "\n".join(lines)
        assert "清洗后复检" in text
        assert "exp1: ✅ clean" in text

    def test_cli_defaults_to_dry_run(self, exp1, capsys):
        """改账本必须是显式动作：不加 --apply 绝不写盘。"""
        from astock.cli.main import main

        _write(exp1,
               [row("2026-07-09 14:45:20"), row("2026-07-09 14:45:26")],
               {"cash": 989_994.90, "init_cash": 1_000_000.0,
                "positions": {"600519": {"qty": 1000, "available": 0, "cost": 10.005}}})
        before = exp1.trades.read_bytes()

        assert main(["clean-ghosts"]) == 0
        assert "预演模式" in capsys.readouterr().out
        assert exp1.trades.read_bytes() == before

    def test_cli_apply_writes(self, exp1, capsys):
        from astock.cli.main import main

        _write(exp1,
               [row("2026-07-09 14:45:20"), row("2026-07-09 14:45:26")],
               {"cash": 989_994.90, "init_cash": 1_000_000.0,
                "positions": {"600519": {"qty": 1000, "available": 0, "cost": 10.005}}})

        assert main(["clean-ghosts", "--apply"]) == 0
        assert "已清洗" in capsys.readouterr().out
        assert len(files.read_csv_rows(exp1.trades)) == 1
