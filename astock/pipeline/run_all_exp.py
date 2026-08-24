"""
批量运行所有实验组。
用法: python3 run_all_exp.py [--force] [--no-jitter]
"""
import sys
import random
import time
from astock.pipeline import run_exp
from astock.core import experiments as exp_manager


def run_all(force=False, no_jitter=False):
    """顺序运行所有实验组"""
    exps = exp_manager.list_experiments()

    if not exps:
        print("暂无实验组配置")
        return

    print(f"共 {len(exps)} 个实验组待运行")
    print("-" * 60)

    # 全局随机抖动（只抖一次）
    if not no_jitter and not force:
        jitter = random.randint(60, 300)  # 1-5分钟
        print(f"[global jitter] 随机延时 {jitter}s 后启动所有实验组...")
        time.sleep(jitter)

    results = []
    for exp in exps:
        exp_id = exp['id']
        print(f"\n{'='*60}")
        print(f"开始运行: {exp_id} - {exp['name']}")
        print(f"{'='*60}")

        try:
            log = run_exp.run_experiment(exp_id, force=force, verbose=True)
            results.append({"id": exp_id, "status": "ok", "log": log})
        except Exception as e:
            print(f"错误: {e}")
            results.append({"id": exp_id, "status": "error", "error": str(e)})

        # 实验组之间短暂间隔，避免并发请求
        time.sleep(2)

    print(f"\n{'='*60}")
    print("所有实验组运行完成")
    print(f"{'='*60}")

    # 打印汇总
    print("\n运行结果汇总:")
    for r in results:
        status = "✓" if r["status"] == "ok" else "✗"
        print(f"  {status} {r['id']}")


if __name__ == "__main__":
    from astock.runtime import clock as market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    force = "--force" in sys.argv
    no_jitter = "--no-jitter" in sys.argv
    run_all(force=force, no_jitter=no_jitter)
