"""
主循环：推进一轮交易。
执行一次 = 推进一个 tick。由定时任务在交易时段周期触发，即“一直跑”。
流程：跨日结算(T+1解冻) -> 拉实时行情 -> 生成信号 -> 下单(规则引擎兜底校验) -> 记录权益快照。
非交易时段默认跳过下单，仅记录（可用 --force 强制，用于测试）。
"""
import sys
import random
import time
import datetime as dt

import market
import broker
import strategy
import market_time


def run_once(force=False, verbose=True):
    log = []
    market_time.enforce()   # 幂等：确保写账本前进程时区=交易所时区
    strategy.clear_indicator_cache()   # 轮首清指标缓存，保证本轮取到最新日线
    def out(m):
        log.append(m)
        if verbose:
            print(m)

    now = dt.datetime.now()
    trading, status = market.is_trading_now(now)
    out(f"=== 推进一轮 {now.strftime('%Y-%m-%d %H:%M:%S')} | 市场: {status} ===")

    st = broker.load_state()
    if broker.settle_new_day(st):
        out("跨日结算：T+1 冻结份额已解冻为可用。")

    if not trading and not force:
        # 非交易时段：只更新一次权益快照（用昨收/最近价估值），不下单
        pool = strategy.load_pool()
        quotes = market.get_quotes(list(set(pool) | set(st["positions"].keys())))
        total, ret = broker.snapshot_equity(st, quotes)
        broker.save_state(st)
        out(f"非交易时段，跳过下单。当前总资产 {total:,.2f}，累计收益 {ret}%")
        return "\n".join(log)

    # 交易时段：取全池+持仓的实时行情
    codes = list(set(strategy.load_pool()) | set(st["positions"].keys()))
    quotes = market.get_quotes(codes)

    # 记录每只票的多源价差，供收盘后校准 DIVERGE_TOL 阈值
    market.log_spread(quotes)

    # 价差采样：用大采样池(沪深300)批量双源刷价差，最大化校准样本（不参与下单）
    try:
        n, _ = market.sample_spreads()
        if n:
            out(f"价差采样：沪深300采集 {n} 只有效双源样本，已记入 spread_log.csv")
    except Exception as e:
        out(f"价差采样跳过：{repr(e)[:80]}")

    # 行情质量体检：显式报告取价失败/脏数据/多源分歧的票，杜绝静默部分失败
    bad, warn = [], []
    for c, q in quotes.items():
        if q.get("error") or q.get("dirty") or q.get("diverge") or q.get("price", 0) <= 0:
            bad.append(c)
        elif str(q.get("cross", "")).startswith("single_source"):
            warn.append(c)
    if bad:
        details = []
        for c in bad:
            q = quotes[c]
            why = q.get("diverge") or q.get("dirty") or q.get("error") or "现价0(休市/拉取失败)"
            details.append(f"{c}({why})")
        out(f"⚠ 行情异常 {len(bad)}/{len(quotes)} 只，本轮跳过：" + "; ".join(details))
    if warn:
        out(f"⚠ 单源降级 {len(warn)} 只（仅1源可用，已放行但风险提高）：" + ", ".join(warn))

    signals = strategy.generate_signals(st, quotes)
    out(f"生成信号 {len(signals)} 条")

    # 先卖后买（腾资金）
    for s in sorted(signals, key=lambda x: 0 if x["action"] == "sell" else 1):
        q = quotes.get(s["code"])
        if not q:
            continue
        if s["action"] == "buy":
            ok, msg = broker.buy(st, q, s["qty"], s["reason"])
        else:
            ok, msg = broker.sell(st, q, s["qty"], s["reason"])
        out(("  ✓ " if ok else "  ✗ ") + msg)

    # 只有真正走完下单分支才会到这里。记录当天日期，供 freshness_gate 的
    # stalled_engine 检查识别"进程在跑但从未进入下单分支"的停摆（2026-07-31 事故）。
    st["round"] = st.get("round", 0) + 1
    st["last_trading_round_date"] = now.strftime("%Y-%m-%d")
    total, ret = broker.snapshot_equity(st, quotes)
    broker.save_state(st)
    out(f"本轮结束 #{st['round']}。总资产 {total:,.2f}，累计收益 {ret}%，现金 {st['cash']:,.2f}")
    return "\n".join(log)


if __name__ == "__main__":
    import market_time
    market_time.enforce()          # 账本日期/时间戳按交易所时区落盘
    market_time.verify()           # 时区没钉住就大声告警，不静默降级
    import os
    force = "--force" in sys.argv
    # 随机抖动：避免整点齐发请求，分散对行情站点的压力，降低失败率。
    # 范围 1~9 分钟（最多540s）——留足余量给本轮约18s的实跑+重试，
    # 确保整条命令稳在调度器单条 Bash 的 10 分钟上限内，不被中途杀掉。
    # --force 或 --no-jitter 时关闭（手动/测试用）。
    JLOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jitter_log.csv")
    def _jlog(row):
        new = not os.path.exists(JLOG)
        with open(JLOG, "a", encoding="utf-8") as f:
            if new:
                f.write("唤醒时刻,计划延时s,实际开跑时刻,实际延时s,状态\n")
            f.write(",".join(str(x) for x in row) + "\n")

    if "--no-jitter" not in sys.argv and not force:
        wake = dt.datetime.now()
        lo = int(os.environ.get("JITTER_MIN", "60"))
        hi = int(os.environ.get("JITTER_MAX", "540"))
        wait = random.randint(lo, hi)
        print(f"[jitter] 随机延时 {wait}s（{wait//60}分{wait%60}秒）后开跑，避开整点高峰…")
        # 睡前先落一行"计划"，若进程被超时杀死则该行无 fire 时刻 -> 可判定截断
        _jlog([wake.strftime("%H:%M:%S"), wait, "", "", "sleeping"])
        time.sleep(wait)
        fire = dt.datetime.now()
        actual = (fire - wake).total_seconds()
        # 睡醒后补一行"已开跑"，含实际延时（用于核对是否 = 计划值）
        _jlog([wake.strftime("%H:%M:%S"), wait,
               fire.strftime("%H:%M:%S"), round(actual, 1), "fired"])
    run_once(force=force)
