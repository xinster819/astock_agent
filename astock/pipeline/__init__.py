"""pipeline · 轮次编排：把 data / strategy / guards / core 串成一次可重放的交易轮。

规则组走 `round_engine`，Agent 组走 `prepare` → (agent 回合) → `execute`。
"""
