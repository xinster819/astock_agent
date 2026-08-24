"""files · 通用落盘工具：原子 JSON 写、CSV 追加与读取。

放在 runtime 而不是 core，是因为这里**没有任何业务语义**——
它不知道什么是账本、什么是成交。core 层的 `ledger` 在此之上定义列含义，
`runtime.jitter` 也用同一套 CSV 追加逻辑写自己的调度日志。
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: Any) -> None:
    """原子写 JSON：临时文件 + fsync + os.replace。

    临时文件必须与目标**同目录**——跨文件系统的 os.replace 不是原子操作。

    为什么非要原子：本系统跑在 launchd 定时任务里，调度器对单条命令有 10 分钟
    硬上限、超时直接 SIGKILL。旧实现 `open(path,"w")` 直接 dump，在 dump 中途
    被杀就会留下截断的 JSON —— 账户的唯一真相当场损坏。
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


def read_json(path: Path, default: Any = None) -> Any:
    """只读 JSON。文件不存在或坏掉返回 default。

    ⚠ 报表层专用：它必须能读一个**不存在**的账户而不产生任何副作用。
    `Account.open()` 在账本缺失时会初始化并落盘——那是交易入口的正确行为，
    但报表调用它就等于"看一眼报表就把 13 个账户全开了户"。
    """
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def append_csv_row(path: Path, columns: list[str], row: list[Any]) -> None:
    """向 CSV 追加一行，文件不存在时先写表头。转义交给 csv 模块。

    ⚠ 绝不要退回 `",".join(str(x))`。读侧用的是 `csv.DictReader`，
    两边转义规则必须严格对称——否则字段里的一个逗号就让整行列错位，
    而 DictReader 不会报错，只会静默给出错位的数据。
    """
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
