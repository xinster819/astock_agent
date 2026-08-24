"""
实验组管理模块：支持多组并行策略对比。
每组独立资金、独立持仓、独立配置，共享行情数据。
"""
import json
import os

EXPERIMENTS_DIR = os.path.join(os.path.dirname(__file__), "experiments")

# 实验组列表
EXPERIMENTS = {
    "exp1": "exp1_baseline.json",
    "exp2": "exp2_loose.json",
    "exp3": "exp3_strict.json",
    "exp4": "exp4_golden_cross.json",
    "exp5": "exp5_momentum.json",
    "exp6": "exp6_regime_trend.json",
    "exp7": "exp7_mean_reversion.json",
    "exp8": "exp8_quality_breakout.json",
    "exp9": "exp9_factor_rank.json",
}


def get_exp_config(exp_id):
    """获取实验组配置"""
    if exp_id not in EXPERIMENTS:
        return None
    config_path = os.path.join(EXPERIMENTS_DIR, EXPERIMENTS[exp_id])
    if not os.path.exists(config_path):
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_exp_state_path(exp_id):
    """获取实验组状态文件路径"""
    return os.path.join(EXPERIMENTS_DIR, f"{exp_id}_state.json")


def get_exp_trades_path(exp_id):
    """获取实验组交易记录路径"""
    return os.path.join(EXPERIMENTS_DIR, f"{exp_id}_trades.csv")


def get_exp_equity_path(exp_id):
    """获取实验组权益记录路径"""
    return os.path.join(EXPERIMENTS_DIR, f"{exp_id}_equity.csv")


def load_exp_state(exp_id):
    """加载实验组状态"""
    state_path = get_exp_state_path(exp_id)
    config = get_exp_config(exp_id)
    if not config:
        return None

    if not os.path.exists(state_path):
        # 初始化状态
        import datetime as dt
        st = {
            "cash": config.get("cash", 1000000.0),
            "init_cash": config.get("init_cash", 1000000.0),
            "positions": {},
            "created": dt.datetime.now().strftime("%Y-%m-%d"),
            "last_settle_date": dt.datetime.now().strftime("%Y-%m-%d"),
            "round": 0,
            "exp_id": exp_id,
        }
        save_exp_state(exp_id, st)
        return st

    with open(state_path, "r", encoding="utf-8") as f:
        st = json.load(f)
        st["exp_id"] = exp_id
        return st


def save_exp_state(exp_id, st):
    """保存实验组状态"""
    state_path = get_exp_state_path(exp_id)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def list_experiments():
    """列出所有实验组"""
    result = []
    for exp_id in EXPERIMENTS:
        config = get_exp_config(exp_id)
        state = load_exp_state(exp_id)
        if config and state:
            result.append({
                "id": exp_id,
                "name": config.get("name", exp_id),
                "desc": config.get("desc", ""),
                "round": state.get("round", 0),
                "cash": state.get("cash", 0),
                "total": state.get("cash", 0) + sum(
                    p.get("qty", 0) * p.get("cost", 0)
                    for p in state.get("positions", {}).values()
                ),
            })
    return result
