"""
市场状态(regime)集中化计算 —— 单一事实源，替代 execute.py / run_exp.py 里各自
重复的、"一报错就 risk_off"的兜底实现。

设计原则（针对旧机制的两个坑）：
  坑① 取数用错接口：旧代码对指数 000300 调用【个股】接口 stock_zh_a_hist，
       必然抛 JSONDecodeError。现改走 market.get_index_hist（指数专用，多源兜底）。
  坑② 异常处置不合理：旧代码任何异常 → 直接 risk_off，且静默、无记忆。
       数据源一断，全系统就被永久冻结新开仓，且"假 risk_off"无法与"真 risk_off"区分。

新的异常处置分级（从好到坏）：
  1) 成功算出         → 用真实 regime，写入 last-known-good 缓存（带时间戳）。
  2) 本次取数失败，但  → 回退到"最近一次成功的真实观测"（缓存未过期，默认 3 天内）。
     有新鲜缓存           这是真实历史数据，不是凭空 risk_off。
  3) 取数失败且无可用   → 冷启动/长期断源，才用保守默认，并【显式标记 degraded】，
     缓存                 让 dashboard/复盘一眼看出"这是降级值，不是真实信号"。

对外只暴露 classify() -> RegimeResult(regime, source, degraded, detail)。
纯 stdlib + market 模块，可被 execute/run_exp import，也可独立体检。
"""
import os
import json
import statistics
import datetime as dt
from dataclasses import dataclass, asdict

from astock.data import market
from astock.guards import risk as risk_guard

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE, "market_regime_cache.json")

INDEX_CODE = "000300"          # 沪深300 作为大盘 beta 基准
LOOKBACK_DAYS = 120            # 取近 120 自然日日线
MIN_BARS = 22                  # 至少 22 个交易日才够算 20 日窗口
CACHE_TTL_HOURS = 72           # last-known-good 最长可信 3 天（约 3 个交易日）
COLD_START_DEFAULT = "risk_off"  # 无任何数据时的保守默认（宁可不开仓）


@dataclass
class RegimeResult:
    regime: str          # normal / high_volatility / risk_off
    source: str          # live / cache / cold_start_default
    degraded: bool       # True = 非本次真实计算，调用方/看板应标红
    detail: str          # 人类可读的说明（含指标值或降级原因）
    metrics: dict = None  # 结构化底层指标(index_return_20d/volatility_20d/drawdown_from_peak/asof…)，冷启动为空

    def as_dict(self):
        return asdict(self)


def _compute_live():
    """真正算一次 regime。成功返回 (regime, detail_dict)，失败抛异常。"""
    end = dt.datetime.now().strftime("%Y%m%d")
    start = (dt.datetime.now() - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    df = market.get_index_hist(INDEX_CODE, start=start, end=end)
    closes = [float(x) for x in df["收盘"].tolist() if float(x) > 0]
    if len(closes) < MIN_BARS:
        raise ValueError(f"指数历史不足 {MIN_BARS} 根（拿到 {len(closes)}）")
    daily = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    index_return = closes[-1] / closes[-21] - 1
    volatility = statistics.pstdev(daily[-20:])
    drawdown = closes[-1] / max(closes) - 1
    regime = risk_guard.classify_market_regime(index_return, volatility, drawdown)
    detail = {
        "index_return_20d": round(index_return, 4),
        "volatility_20d": round(volatility, 4),
        "drawdown_from_peak": round(drawdown, 4),
        "bars": len(closes),
        "asof": df["日期"].tolist()[-1],
    }
    return regime, detail


def _read_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(regime, detail):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "regime": regime,
                "detail": detail,
                "computed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 缓存写失败不阻塞主流程


def classify(cold_start_default=COLD_START_DEFAULT, now=None):
    """返回 RegimeResult。这是唯一对外入口。

    cold_start_default: 无任何可用数据时的保守默认（策略配置可覆盖）。
    """
    now = now or dt.datetime.now()

    # ---- 1) 优先真实计算 ----
    try:
        regime, detail = _compute_live()
        _write_cache(regime, detail)
        return RegimeResult(
            regime=regime, source="live", degraded=False,
            detail=(f"实时计算：20日{detail['index_return_20d']:+.2%} "
                    f"波动{detail['volatility_20d']:.2%} "
                    f"回撤{detail['drawdown_from_peak']:+.2%}（截至{detail['asof']}）"),
            metrics=detail,
        )
    except Exception as live_err:
        live_msg = repr(live_err)[:100]

    # ---- 2) 回退到 last-known-good（真实历史，非凭空）----
    cache = _read_cache()
    if cache:
        try:
            computed_at = dt.datetime.strptime(cache["computed_at"], "%Y-%m-%d %H:%M:%S")
            age_h = (now - computed_at).total_seconds() / 3600
            if age_h <= CACHE_TTL_HOURS:
                return RegimeResult(
                    regime=cache["regime"], source="cache", degraded=True,
                    detail=(f"⚠ 本次指数取数失败（{live_msg}），"
                            f"回退最近一次真实判定 {cache['regime']}"
                            f"（{cache['computed_at']}，{age_h:.1f}h 前）。"),
                    metrics=cache.get("detail") if isinstance(cache.get("detail"), dict) else None,
                )
        except Exception:
            pass  # 缓存损坏，落到冷启动

    # ---- 3) 冷启动/长期断源：保守默认，显式标 degraded ----
    return RegimeResult(
        regime=cold_start_default, source="cold_start_default", degraded=True,
        detail=(f"⚠ 指数取数失败且无新鲜缓存（{live_msg}），"
                f"采用保守默认 {cold_start_default}。这是降级值，非真实市场信号，需尽快修复数据源。"),
    )


if __name__ == "__main__":
    r = classify()
    print("== 市场状态体检 ==")
    print(f"  regime  : {r.regime}")
    print(f"  source  : {r.source}")
    print(f"  degraded: {r.degraded}")
    print(f"  detail  : {r.detail}")
