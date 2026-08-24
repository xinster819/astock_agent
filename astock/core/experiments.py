"""experiments · 规则实验组的**配置**注册表。

【瘦身说明】
重构前这个模块（叫 `exp_manager`）同时管两件事：实验组配置，以及 exp 账户的
账本读写（`load_exp_state` / `save_exp_state` / `get_exp_*_path`）。后者与
`broker` 的 `load_state` / `save_state` 是同一件事的两份实现，初始化逻辑还不一致
（初始现金字段名、created 取法、是否带 exp_id 各写各的）。

账本读写已经统一到 `core.account.Account`，路径统一到 `runtime.paths`。
这里只剩下它真正独有的职责：**哪些实验组存在、各自的参数是什么**。
"""
from __future__ import annotations

import json
from typing import Any

from astock.runtime import paths

#: 实验组 id -> 配置文件名。九种信号族，共用同一套卖出逻辑与仓位约束，
#: 差异全在配置里——这是"对照实验"能成立的前提。
EXPERIMENTS = {
    "exp1": "exp1_baseline.json",          # 基准：上穿 MA20
    "exp2": "exp2_loose.json",             # 放宽：上穿 MA10
    "exp3": "exp3_strict.json",            # 严格：上穿 MA30
    "exp4": "exp4_golden_cross.json",      # 真金叉：要求穿越事件本身
    "exp5": "exp5_momentum.json",          # 纯动量
    "exp6": "exp6_regime_trend.json",      # 市场状态择时
    "exp7": "exp7_mean_reversion.json",    # 均值回归：RSI 超卖 + 中期趋势之上
    "exp8": "exp8_quality_breakout.json",  # 放量确认的突破
    "exp9": "exp9_factor_rank.json",       # 多因子横截面合成分
}


#: signal_logic 名字里已经编码了慢线周期。`ma_slow` 是配置里的冗余字段——
#: 它被读出来但从未参与计算，改它不会有任何效果。九份配置目前恰好都自洽，
#: 所以这个陷阱一直没被踩到。与其留着，不如让矛盾直接报错。
_SLOW_MA_BY_LOGIC = {
    "cross_up_ma10": 10,
    "cross_up_ma20": 20,
    "cross_up_ma30": 30,
    "ma5_cross_ma20": 20,
}


def is_experiment(account_id: str) -> bool:
    return account_id in EXPERIMENTS


class ConfigError(ValueError):
    """实验组配置自相矛盾。宁可开不了盘，也不能让配置默默失效。"""


def validate_config(exp_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """校验配置的内部一致性。返回原配置，不一致则抛 ConfigError。

    只查"改了没用"的字段——这类字段最危险：调参的人以为自己在做对照实验，
    实际上两组跑的是同一套参数，得出的结论是假的。
    """
    logic = config.get("signal_logic")
    declared = config.get("ma_slow")
    expected = _SLOW_MA_BY_LOGIC.get(logic) if isinstance(logic, str) else None
    if declared is not None and expected is not None and int(declared) != expected:
        raise ConfigError(
            f"{exp_id}: ma_slow={declared} 与 signal_logic={logic}（隐含慢线 {expected}）"
            f"矛盾。慢线周期由 signal_logic 决定，ma_slow 不参与计算——"
            f"照 declared 值调参不会有任何效果。请改 signal_logic，或删掉 ma_slow。"
        )
    return config


def get_exp_config(exp_id: str) -> dict[str, Any] | None:
    """读实验组参数。id 未注册或文件缺失都返回 None（由调用方决定如何处理）。"""
    filename = EXPERIMENTS.get(exp_id)
    if not filename:
        return None
    config_path = paths.experiment_config(exp_id, filename)
    if not config_path.exists():
        return None
    with config_path.open("r", encoding="utf-8") as f:
        return validate_config(exp_id, json.load(f))


def list_experiments() -> list[dict[str, Any]]:
    """列出全部实验组及其当前账户概况。配置缺失的组直接跳过，不静默造假。"""
    from astock.core.account import Account

    result = []
    for exp_id in EXPERIMENTS:
        config = get_exp_config(exp_id)
        if not config:
            continue
        account = Account.open(exp_id, init_cash=config.get("init_cash", config.get("cash")))
        state = account.state
        # 这里用成本价估值，不拉实时行情——列表是给调度和 CLI 用的快照，
        # 13 个账户逐个联网取价会把一次 `astock list` 拖到几十秒。
        holdings = sum(p.get("qty", 0) * p.get("cost", 0)
                       for p in state.get("positions", {}).values())
        result.append({
            "id": exp_id,
            "name": config.get("name", exp_id),
            "desc": config.get("desc", ""),
            "round": state.get("round", 0),
            "cash": state.get("cash", 0),
            "total": state.get("cash", 0) + holdings,
        })
    return result
