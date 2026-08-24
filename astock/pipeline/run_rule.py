"""run_rule · 规则组的一轮交易：A 组（对照基线）与 exp1~exp9（实验组）。

两者的唯一区别是**参数**，不是流程：A 组用内置默认参数、不加组合风控（保持
对照基线语义）；exp 组各自读 `config/experiments/exp<N>_*.json`，启用完整闸门。

重构前这两条路径是两个文件（`run.py` 与 `run_exp.py`），共 477 行，
其中包含一整套复制的 buy/sell/费率/账本写入。现在都落到 `round_engine`，
本模块只负责挑参数。
"""
from __future__ import annotations

from typing import Any

from astock.core import experiments
from astock.pipeline.round_engine import RoundPolicy, RoundReport, rule_decider, run_round
from astock.runtime.paths import CONTROL_GROUP


def run_control(*, force: bool = False, verbose: bool = True) -> RoundReport:
    """A 组：纯规则对照基线。

    不启用组合风控——这是刻意的。A 组存在的意义就是提供一条"只有信号、
    没有额外干预"的基准线，给它加风控等于把对照组也变成实验组。
    互斥锁与冷却去抖照常启用：它们只拦重复触发，不改变策略语义。
    """
    return run_round(
        CONTROL_GROUP,
        rule_decider,
        config={"name": "A组·纯规则对照"},
        policy=RoundPolicy.control_group(),
        force=force,
        verbose=verbose,
    )


def run_experiment(exp_id: str, *, force: bool = False,
                   verbose: bool = True) -> RoundReport | None:
    """exp1~exp9：配置驱动的规则实验组。配置不存在返回 None。"""
    config: dict[str, Any] | None = experiments.get_exp_config(exp_id)
    if not config:
        if verbose:
            print(f"错误: 实验组 {exp_id} 不存在")
        return None
    return run_round(
        exp_id,
        rule_decider,
        config=config,
        init_cash=config.get("init_cash", config.get("cash")),
        force=force,
        verbose=verbose,
    )
