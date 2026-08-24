"""ledger · 账本落盘（**唯一** 写 state/trades/equity 的地方）。

【三个被修掉的落盘缺陷】

1. **state.json 非原子写**
   旧实现 `open(path,"w")` 直接 dump。这套系统跑在 launchd 定时任务里，
   调度器对单条命令有 10 分钟硬上限、超时直接 SIGKILL（run.py 的 jitter
   注释里就写着要为此留余量）。一旦在 dump 中途被杀，state.json 只剩半截
   JSON——账户的唯一真相当场损坏，且下一轮 load 会抛异常而不是静默，
   算是不幸中的万幸。现在改为 **写临时文件 → os.replace 原子换名**，
   同目录 rename 在 POSIX 上是原子的，要么旧的完整、要么新的完整。

2. **CSV 写入不转义，读取却按标准 CSV 解析**
   旧实现写用 `",".join(str(x))`，读用 `csv.DictReader`。而 trades 的 `reason`
   列是 **agent 自由文本**（execute.py 把 `decision["reason"]` 直接拼进去）。
   agent 只要写出「止损, 跌破支撑」这样一句，该行就从 10 列变成 11 列，
   DictReader 静默错位 —— 账本重放对账从此对的是错位数据。
   现在统一走 `csv.writer`，转义规则与读侧严格对称。

3. **表头与行拼接分散在各处**
   trades/equity 的列定义原本散在 broker 的两个函数里，改列要改两处且
   没有任何校验。现在列名是模块级常量，写入按列名组装。

本模块只做 IO，不含任何撮合规则——规则在 `rules`，两者由 `account` 组合。
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from astock.core.rules import Fill

#: trades.csv 列定义。顺序即落盘顺序，改动会影响历史文件的可读性——
#: 只允许在末尾追加列，不允许插入或重排。
TRADE_COLUMNS = [
    "时间", "方向", "代码", "名称", "价格", "数量",
    "成交额", "费用", "现金余额", "备注",
]

#: equity.csv 列定义，约束同上。
EQUITY_COLUMNS = ["时间", "现金", "持仓市值", "总资产", "累计收益率%"]


def write_json_atomic(path: Path, payload: Any) -> None:
    """原子写 JSON：临时文件 + fsync + os.replace。

    临时文件必须与目标**同目录**——跨文件系统的 os.replace 不是原子操作。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())      # 元数据落盘前先把内容刷到磁盘
        os.replace(tmp, path)         # POSIX 原子换名
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def append_csv_row(path: Path, columns: list[str], row: list[Any]) -> None:
    """向 CSV 追加一行，文件不存在时先写表头。转义交给 csv 模块。"""
    if len(row) != len(columns):
        raise ValueError(f"{path.name}: 期望 {len(columns)} 列，收到 {len(row)} 列")
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(columns)
        writer.writerow(row)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """读回 CSV。文件不存在返回空列表——账户尚未开张不是错误。"""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class Ledger:
    """一个账户的账本文件读写。路径由 `AccountPaths` 注入，不读环境变量。"""

    def __init__(self, paths: Any) -> None:
        self.paths = paths

    # ---- state ----

    def state_exists(self) -> bool:
        return self.paths.state.exists()

    def load_state(self) -> dict[str, Any]:
        with self.paths.state.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save_state(self, state: dict[str, Any]) -> None:
        write_json_atomic(self.paths.state, state)

    # ---- trades ----

    def append_fill(self, fill: Fill, timestamp: str) -> None:
        """记一笔成交。卖出的已实现盈亏并进备注列，与历史文件格式保持一致。"""
        note = fill.reason
        if fill.realized_pnl is not None:
            note = f"{fill.reason} 盈亏{fill.realized_pnl}"
        append_csv_row(self.paths.trades, TRADE_COLUMNS, [
            timestamp, fill.side, fill.code, fill.name, fill.price, fill.qty,
            fill.amount, fill.fee, fill.cash_after, note,
        ])

    def read_trades(self) -> list[dict[str, str]]:
        return read_csv_rows(self.paths.trades)

    # ---- equity ----

    def append_equity(self, timestamp: str, cash: float,
                      market_value: float, total: float, return_pct: float) -> None:
        append_csv_row(self.paths.equity, EQUITY_COLUMNS,
                       [timestamp, cash, market_value, total, return_pct])

    def read_equity(self) -> list[dict[str, str]]:
        return read_csv_rows(self.paths.equity)
