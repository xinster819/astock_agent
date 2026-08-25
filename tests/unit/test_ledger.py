"""core.ledger · 账本落盘。

这一层的三条承诺，每条都对应一个曾经真实存在的缺陷：
  1. state.json 原子写   —— 进程被 SIGKILL 也不留半截 JSON
  2. CSV 标准转义       —— agent 自由文本里的逗号不会让整行列错位
  3. 表头只写一次       —— 追加模式下不重复表头
"""
import csv
import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest

from astock.core import ledger
from astock.core.rules import Fill
from astock.runtime.paths import AccountPaths


@pytest.fixture
def book(isolated_env):
    return ledger.Ledger(AccountPaths.for_group("A").ensure_dirs())


# =========================================================== 原子写

class TestAtomicJsonWrite:
    """state.json 是账户的唯一真相，写坏了就没了。

    这套系统跑在 launchd 定时任务里，调度器对单条命令有 10 分钟硬上限、
    超时直接 SIGKILL。旧实现 `open(path,"w")` 直接 dump，在 dump 中途被杀
    就会留下截断的 JSON。
    """

    def test_roundtrip(self, book):
        payload = {"cash": 1000.0, "positions": {"600519": {"qty": 100}}}
        book.save_state(payload)
        assert book.load_state() == payload

    def test_leaves_no_temp_files_behind(self, book, isolated_env):
        book.save_state({"cash": 1.0})
        leftovers = [p for p in os.listdir(isolated_env) if p.endswith(".tmp")]
        assert leftovers == []

    def test_previous_content_survives_a_failed_write(self, book):
        """写新内容时抛异常，磁盘上必须仍是**完整的旧内容**，不是半截新内容。"""
        book.save_state({"cash": 1000.0})

        class Unserializable:
            pass

        with pytest.raises(TypeError):
            book.save_state({"cash": Unserializable()})
        assert book.load_state() == {"cash": 1000.0}

    def test_survives_sigkill_midwrite(self, tmp_path):
        """端到端：写到一半被 SIGKILL，旧文件必须完好无损。

        这是本模块存在的全部理由，值得开一个真进程来验。
        """
        target = tmp_path / "state.json"
        target.write_text(json.dumps({"cash": 999.0}), encoding="utf-8")
        script = textwrap.dedent(f"""
            import os, signal, sys
            sys.path.insert(0, {str(os.getcwd())!r})
            from pathlib import Path
            from astock.core.ledger import write_json_atomic

            class Bomb(dict):
                # json 的纯 Python 编码器（indent 非 None 时启用）遍历 dict 走 items()。
                # 必须非空——空 dict 会被编码器短路成 "{{}}" 而不调 items()。
                def items(self):
                    os.kill(os.getpid(), signal.SIGKILL)   # dump 到一半自杀
                    return []

            write_json_atomic(Path({str(target)!r}), {{"positions": Bomb({{"seed": 1}})}})
        """)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
        assert proc.returncode == -signal.SIGKILL, "用例本身没能触发中途中断"
        assert json.loads(target.read_text(encoding="utf-8")) == {"cash": 999.0}


# =========================================================== CSV 转义

class TestTradeCsvEscaping:
    """写用 ",".join、读用 csv.DictReader —— 这就是旧实现的组合。

    trades 的备注列是 agent 自由文本（execute 把 decision["reason"] 直接拼进去），
    agent 写一句「止损, 跌破支撑」，该行就从 10 列变成 11 列，
    DictReader 静默错位，账本重放对账从此对的是错位数据。
    """

    def _fill(self, reason, realized=None):
        return Fill(side="卖出" if realized else "买入", code="600519", name="贵州茅台",
                    price=10.0, qty=100, amount=1000.0, fee=5.01,
                    cash_after=98_994.99, reason=reason, realized_pnl=realized)

    @pytest.mark.parametrize("reason", [
        "止损, 跌破支撑",
        '带"引号"的理由',
        "换行\n也要能扛住",
        "逗号,引号\",换行\n三件套",
    ])
    def test_column_count_survives_hostile_text(self, book, reason):
        book.append_fill(self._fill(reason), timestamp="2026-08-25 10:00:00")
        rows = book.read_trades()
        assert len(rows) == 1
        assert len(rows[0]) == len(ledger.TRADE_COLUMNS)
        assert rows[0]["备注"] == reason
        assert rows[0]["现金余额"] == "98994.99", "列错位会让现金列读成别的东西"

    def test_realized_pnl_is_appended_to_note(self, book):
        book.append_fill(self._fill("止盈", realized=123.45), timestamp="t")
        assert "盈亏123.45" in book.read_trades()[0]["备注"]

    def test_header_written_once(self, book):
        for _ in range(3):
            book.append_fill(self._fill("x"), timestamp="t")
        raw = book.paths.trades.read_text(encoding="utf-8")
        assert raw.count("现金余额") == 1
        assert len(book.read_trades()) == 3

    def test_column_count_mismatch_is_rejected(self, book):
        """列数对不上直接报错，绝不写出一个错位的账本行。"""
        with pytest.raises(ValueError, match="列"):
            ledger.append_csv_row(book.paths.trades, ledger.TRADE_COLUMNS, ["只有一列"])


class TestEquityCsv:

    def test_roundtrip(self, book):
        book.append_equity("2026-08-25 15:00:00", 1000.0, 2000.0, 3000.0, 5.5)
        row = book.read_equity()[0]
        assert row["总资产"] == "3000.0"
        assert row["累计收益率%"] == "5.5"

    def test_missing_file_reads_as_empty(self, book):
        """账户尚未开张不是错误。"""
        assert book.read_equity() == []
        assert book.read_trades() == []


class TestReadWriteSymmetry:
    """写侧和读侧必须用同一套转义规则——这正是旧实现出问题的地方。"""

    def test_writer_output_is_parseable_by_stdlib_reader(self, book):
        nasty = 'a,b"c\nd'
        ledger.append_csv_row(book.paths.equity, ledger.EQUITY_COLUMNS,
                              ["t", 1, 2, 3, nasty])
        with book.paths.equity.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["累计收益率%"] == nasty
