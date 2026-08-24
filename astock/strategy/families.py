"""families · 9 种买入信号族的注册表。

【为什么从 generate_signals 里拆出来】
重构前这 8 个分支挤在一个 232 行函数中间的 if/elif 链里，与"共用的卖出逻辑、
仓位再平衡、下单量计算"缠在一起。代价有三：

  1. **测不动**：想验证 mean_reversion 的一条判据，得把整个 generate_signals
     连同持仓、行情、再平衡一起造出来。
  2. **加不动**：加第 10 个信号族要去改那条 if/elif 链，改错会波及其他 8 个。
  3. **对照实验的重心跑偏**：这个项目的价值在于九组之间的差异，而差异恰恰
     被埋在最难看清的地方。

现在每个族是一个纯函数：拿到 `SignalContext`，回答"这只票该不该买"。
一个族一段独立的注释说明它在赌什么、以及为什么这么写。

【未注册的 signal_logic 会抛错，不会静默】
旧实现里 if/elif 全部落空就是 `should_buy = False`——配置里把 `mean_reversion`
拼成 `mean_reversal`，表现是该账户**对全池所有票都不买**，永久静默停止交易，
而权益曲线照常写、闸门全绿。这与 2026-07-31 停摆是同一类故障。
所以这里宁可开不了盘：`resolve()` 直接抛 `UnknownSignalFamily`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: 信号族：给定上下文，回答"这只票本轮该不该买"。
SignalFamily = Callable[["SignalContext"], bool]

_REGISTRY: dict[str, SignalFamily] = {}


class UnknownSignalFamily(KeyError):
    """配置里的 signal_logic 没有对应实现。宁可开不了盘，也不静默不开仓。"""


@dataclass(frozen=True)
class SignalContext:
    """一只候选票的全部判断依据。

    `ind` 是 `signals._indicators()` 的返回值，允许被就地写入派生字段
    （factor_rank 会往里写 `factor_score` 供后续排序），其余字段只读。
    """

    ind: dict[str, Any]
    cfg: dict[str, Any]
    momentum_threshold: float

    @property
    def bullish(self) -> bool:
        """多头排列：MA5 在 MA20 之上。多数信号族的公共前置条件。"""
        ma5, ma20 = self.ind.get("ma5"), self.ind.get("ma20")
        return bool(ma5 and ma20 and ma5 > ma20)

    @property
    def momentum_ok(self) -> bool:
        return self.ind["momentum"] > self.momentum_threshold

    def option(self, key: str, default: Any) -> Any:
        return self.cfg.get(key, default)


def family(name: str) -> Callable[[SignalFamily], SignalFamily]:
    """把一个函数注册为信号族。重名直接报错——静默覆盖会让对照实验失真。"""
    def register(fn: SignalFamily) -> SignalFamily:
        if name in _REGISTRY:
            raise ValueError(f"信号族 {name} 重复注册")
        _REGISTRY[name] = fn
        return fn
    return register


def resolve(name: str) -> SignalFamily:
    """按名字取信号族。未注册直接抛错，绝不退化成"什么都不买"。"""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownSignalFamily(
            f"未知信号族 {name!r}。已注册：{sorted(_REGISTRY)}。"
            f"配置里 signal_logic 拼错会让该账户对全池所有票都不买——"
            f"表现为永久静默停止交易，而权益曲线照常写、闸门全绿。"
        ) from None


def registered() -> list[str]:
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# 均线穿越族：赌的是"趋势刚刚确立"这个事件
# ---------------------------------------------------------------------------

@family("cross_up_ma20")
def cross_up_ma20(ctx: SignalContext) -> bool:
    """基准：上穿 MA20 + 多头排列 + 动量达标。九组的对照原点。"""
    return bool(ctx.ind["cross_up_ma20"]) and ctx.bullish and ctx.momentum_ok


@family("cross_up_ma10")
def cross_up_ma10(ctx: SignalContext) -> bool:
    """放宽：慢线换成 MA10，信号更多也更噪。"""
    ma10, prev_close = ctx.ind["ma10"], ctx.ind["prev_close"]
    if not ma10:
        return False
    crossed = prev_close <= ma10 and ctx.ind["close"] > ma10
    return crossed and ctx.bullish and ctx.momentum_ok


@family("cross_up_ma30")
def cross_up_ma30(ctx: SignalContext) -> bool:
    """严格：慢线换成 MA30。

    ⚠ MA30 不在 `_indicators` 的常规产物里，需要现取日线现算——
    这是唯一一个会在信号判断时联网的族。取数失败必须显式告警：
    静默返回 False 会让 exp3 整轮无买入信号，与"确实没信号"无法区分。
    """
    from astock.strategy import signals as _signals

    closes = _signals.recent_closes(ctx.ind["code"], bars=31)
    if not closes:
        return False
    ma30 = _signals._ma(closes, 30)
    prev_ma30 = _signals._ma(closes[:-1], 30)
    if not prev_ma30 or not ma30:
        return False
    crossed = closes[-2] <= prev_ma30 and closes[-1] > ma30
    return crossed and ctx.bullish and ctx.momentum_ok


@family("ma5_cross_ma20")
def ma5_cross_ma20(ctx: SignalContext) -> bool:
    """真金叉：要求 MA5 上穿 MA20 的**穿越事件本身**。

    旧实现只判当前 ma5 > ma20（即"已多头就买"），任何早已多头、冲高很久的票
    都会触发买入——exp4 曾以 @831 的价格接盘北方华创、动量已达 41%。
    `golden_cross` 由指标层依据历史序列判定上一周期是否还在 MA20 之下。
    """
    return bool(ctx.ind.get("golden_cross")) and ctx.momentum_ok


# ---------------------------------------------------------------------------
# 状态族：赌的是"当前所处的状态"，而非某个瞬时事件
# ---------------------------------------------------------------------------

@family("pure_momentum")
def pure_momentum(ctx: SignalContext) -> bool:
    """纯动量。仍要求趋势与量能确认，避免在弱市里追逐反弹。"""
    volume_ratio = ctx.ind.get("volume_ratio")
    volume_ok = volume_ratio is None or volume_ratio >= ctx.option("min_volume_ratio", 1.0)
    return ctx.momentum_ok and ctx.bullish and volume_ok


@family("mean_reversion")
def mean_reversion(ctx: SignalContext) -> bool:
    """均值回归：短期超卖但仍在中期趋势之上，等回归而不是追涨。

    与穿越族正交——它专门买那些穿越族**不会买**的票（正在回调的强势股）。
    `ma20_floor` 控制允许回调多深仍视为"趋势未破"。
    """
    rsi14 = ctx.ind.get("rsi14")
    if rsi14 is None:
        return False
    oversold = rsi14 <= ctx.option("rsi_buy_max", 35)
    above_floor = ctx.ind["close"] >= ctx.ind["ma20"] * ctx.option("ma20_floor", 0.97)
    return oversold and above_floor and ctx.ind["momentum"] >= ctx.momentum_threshold


@family("quality_breakout")
def quality_breakout(ctx: SignalContext) -> bool:
    """质量突破：趋势 + MA20 突破 + 放量 + 温和动量，四者同时确认。

    `breakout_relaxed=True` 把"当日上穿 MA20"（同 bar 事件，罕见到曾致全周 0 单）
    放宽为"站上/上穿 MA20"；但放量、动量、多头排列仍是硬门槛，
    放宽的只是触发时机，不是质量要求。
    """
    ma20 = ctx.ind.get("ma20")
    above_ma20 = bool(ma20 and ctx.ind["close"] >= ma20)
    breakout_ok = above_ma20 if ctx.option("breakout_relaxed", False) \
        else bool(ctx.ind["cross_up_ma20"])
    volume_ratio = ctx.ind.get("volume_ratio")
    volume_ok = volume_ratio is None or volume_ratio >= ctx.option("min_volume_ratio", 1.1)
    return breakout_ok and ctx.bullish and ctx.momentum_ok and volume_ok


@family("factor_rank")
def factor_rank(ctx: SignalContext) -> bool:
    """多因子横截面排序（借鉴 Qlib 因子模型思想，纯 stdlib 实现）。

    与事件触发型策略正交：不赌某个穿越/突破事件，而是先让所有过了基本门槛
    （多头 + 动量达标 + 未超买）的票入围，再用合成因子分在候选集里择优。

    副作用：给 `ind` 写入 `factor_score`，供上层排序时择优。这是刻意的——
    分数只有在通过门槛后才有意义，算了也白算。
    """
    rsi14 = ctx.ind.get("rsi14")
    not_overbought = rsi14 is None or rsi14 <= ctx.option("rsi_overbought", 72)
    if not (ctx.bullish and ctx.momentum_ok and not_overbought):
        return False

    weights = ctx.option("factor_weights", {})
    volume_ratio = ctx.ind.get("volume_ratio")
    # 量能贡献：相对成交量越高越好，封顶 2.0 后归一化到 [0,1]
    volume_component = (min(volume_ratio, 2.0) / 2.0) if volume_ratio else 0.0
    # RSI 适中度：偏离 55 越远得分越低，同时惩罚超买与超卖两端
    rsi_component = 1.0 - min(abs((rsi14 if rsi14 is not None else 55) - 55) / 45.0, 1.0)
    # 偏离度惩罚：离 MA20 越远越像追高
    ma20 = ctx.ind.get("ma20")
    distance = (ctx.ind["close"] / ma20 - 1) if ma20 else 0.0

    ctx.ind["factor_score"] = (
        weights.get("momentum", 1.0) * ctx.ind["momentum"]
        + weights.get("volume", 0.3) * volume_component
        + weights.get("rsi_mid", 0.2) * rsi_component
        - weights.get("distance_penalty", 0.5) * max(distance, 0.0)
    )
    return True
