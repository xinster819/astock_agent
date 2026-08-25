"""strategy.families · 9 种买入信号族。

这些判据以前埋在一个 232 行函数中间的 if/elif 链里，想验证其中一条就得把整个
generate_signals 连同持仓、行情、再平衡一起造出来。拆成注册表之后，
每个族都是一个纯函数——下面每个用例只回答一句话："这只票该不该买"。

**这个项目的价值在于九组之间的差异**，所以这里重点测的是每个族
"买什么、以及**不**买什么"，尤其是它与其他族分歧的地方。
"""
import pytest

from astock.strategy import families
from astock.strategy.families import SignalContext


def ctx(momentum_threshold=0.0, cfg=None, **indicators):
    """构造一个信号上下文。默认给出一只健康的多头票。"""
    ind = {
        "code": "600519", "close": 11.0, "prev_close": 10.5,
        "ma5": 10.8, "ma10": 10.5, "ma20": 10.0, "prev_ma20": 9.9,
        "momentum": 0.05, "cross_up_ma20": False, "below_ma10": False,
        "golden_cross": False, "rsi14": 55.0, "volume_ratio": 1.5,
    }
    ind.update(indicators)
    return SignalContext(ind=ind, cfg=cfg or {}, momentum_threshold=momentum_threshold)


class TestRegistry:

    def test_all_configured_families_are_implemented(self):
        """配置里用到的每个 signal_logic 都必须有实现——否则该组永久不开仓。"""
        from astock.core import experiments

        for exp_id in experiments.EXPERIMENTS:
            logic = experiments.get_exp_config(exp_id)["signal_logic"]
            families.resolve(logic)      # 未注册会抛 UnknownSignalFamily

    def test_unknown_family_raises_instead_of_silently_not_buying(self):
        """配置拼错必须炸掉。

        旧实现里 if/elif 全部落空就是 should_buy=False——把 mean_reversion
        拼成 mean_reversal，表现是该账户对全池所有票都不买，永久静默停止交易，
        而权益曲线照常写、闸门全绿。与 2026-07-31 停摆是同一类故障。
        """
        with pytest.raises(families.UnknownSignalFamily) as exc:
            families.resolve("mean_reversal")
        assert "已注册" in str(exc.value), "报错要告诉人正确的名字有哪些"

    def test_duplicate_registration_is_refused(self):
        """静默覆盖会让两个实验组跑同一套逻辑，对照失真。"""
        with pytest.raises(ValueError, match="重复注册"):
            families.family("cross_up_ma20")(lambda c: True)


class TestSignalContext:

    def test_bullish_requires_ma5_above_ma20(self):
        assert ctx(ma5=11.0, ma20=10.0).bullish
        assert not ctx(ma5=9.0, ma20=10.0).bullish

    def test_bullish_is_false_when_indicators_missing(self):
        """指标缺失不等于看多。宁可不买。"""
        assert not ctx(ma5=None).bullish
        assert not ctx(ma20=None).bullish


class TestCrossUpMa20:
    """基准族：九组的对照原点。"""

    def test_buys_on_cross_with_trend_and_momentum(self):
        assert families.cross_up_ma20(ctx(cross_up_ma20=True))

    def test_requires_the_crossing_event(self):
        assert not families.cross_up_ma20(ctx(cross_up_ma20=False))

    def test_requires_bullish_alignment(self):
        assert not families.cross_up_ma20(ctx(cross_up_ma20=True, ma5=9.0, ma20=10.0))

    def test_requires_momentum_above_threshold(self):
        assert not families.cross_up_ma20(
            ctx(cross_up_ma20=True, momentum=0.01, momentum_threshold=0.05))


class TestCrossUpMa10:
    """放宽族：慢线换 MA10，信号更多也更噪。"""

    def test_buys_when_close_crosses_above_ma10(self):
        assert families.cross_up_ma10(ctx(prev_close=10.4, close=10.6, ma10=10.5))

    def test_does_not_buy_when_already_above(self):
        """上一周期就在 MA10 之上 = 不是穿越。"""
        assert not families.cross_up_ma10(ctx(prev_close=10.6, close=10.7, ma10=10.5))

    def test_missing_ma10_does_not_buy(self):
        assert not families.cross_up_ma10(ctx(ma10=None))


class TestMa5CrossMa20:
    """真金叉族：修「假金叉」的那一个。"""

    def test_requires_the_crossing_event_not_merely_bullish(self):
        """已多头很久的票不该买——exp4 曾以 @831 接盘北方华创、动量已达 41%。"""
        already_bullish = ctx(golden_cross=False, ma5=15.0, ma20=10.0, momentum=0.41)
        assert not families.ma5_cross_ma20(already_bullish)

    def test_buys_on_a_real_cross(self):
        assert families.ma5_cross_ma20(ctx(golden_cross=True))

    def test_weak_momentum_rejected_even_on_a_real_cross(self):
        assert not families.ma5_cross_ma20(
            ctx(golden_cross=True, momentum=0.001, momentum_threshold=0.02))


class TestPureMomentum:

    def test_requires_trend_confirmation(self):
        """弱市里的反弹不买——动量高但趋势没起来时拒绝。"""
        assert not families.pure_momentum(ctx(momentum=0.20, ma5=9.0, ma20=10.0))

    def test_requires_volume_confirmation(self):
        assert not families.pure_momentum(
            ctx(volume_ratio=0.5), )
        assert families.pure_momentum(ctx(volume_ratio=1.5))

    def test_absent_volume_data_does_not_block(self):
        """量能数据缺失不该一票否决——那会让整族因数据问题静默停摆。"""
        assert families.pure_momentum(ctx(volume_ratio=None))


class TestMeanReversion:
    """与穿越族**正交**：专买穿越族不会买的票（正在回调的强势股）。"""

    def test_buys_oversold_still_above_midterm_trend(self):
        assert families.mean_reversion(ctx(rsi14=30.0, close=9.9, ma20=10.0))

    def test_rejects_when_midterm_trend_is_broken(self):
        """跌穿 ma20_floor = 趋势已破，不是回调而是转势。"""
        assert not families.mean_reversion(ctx(rsi14=30.0, close=9.0, ma20=10.0))

    def test_rejects_when_not_oversold(self):
        assert not families.mean_reversion(ctx(rsi14=60.0, close=9.9, ma20=10.0))

    def test_wider_floor_admits_deeper_pullback(self):
        deeper = ctx(rsi14=30.0, close=9.3, ma20=10.0, cfg={"ma20_floor": 0.92})
        assert families.mean_reversion(deeper)

    def test_missing_rsi_does_not_buy(self):
        """这一族的全部判据就建立在 RSI 上，没有它就没有依据。"""
        assert not families.mean_reversion(ctx(rsi14=None))


class TestQualityBreakout:

    def test_requires_volume_confirmation(self):
        assert not families.quality_breakout(ctx(cross_up_ma20=True, volume_ratio=1.0))
        assert families.quality_breakout(ctx(cross_up_ma20=True, volume_ratio=1.5))

    def test_strict_mode_requires_the_same_bar_cross(self):
        """未放宽时要求"当日上穿"这个同 bar 事件——罕见到曾致全周 0 单。"""
        assert not families.quality_breakout(ctx(cross_up_ma20=False, close=11.0, ma20=10.0))

    def test_relaxed_mode_accepts_standing_above_ma20(self):
        relaxed = ctx(cross_up_ma20=False, close=11.0, ma20=10.0,
                      cfg={"breakout_relaxed": True})
        assert families.quality_breakout(relaxed)

    def test_relaxed_mode_still_requires_volume(self):
        """放宽的只是触发时机，不是质量要求。"""
        weak = ctx(cross_up_ma20=False, close=11.0, ma20=10.0, volume_ratio=0.8,
                   cfg={"breakout_relaxed": True})
        assert not families.quality_breakout(weak)


class TestFactorRank:
    """横截面族：不赌事件，而是在过了门槛的候选里择优。"""

    def test_rejects_overbought(self):
        assert not families.factor_rank(ctx(rsi14=80.0, cfg={"rsi_overbought": 72}))

    def test_admits_qualified_and_writes_a_score(self):
        context = ctx(rsi14=55.0)
        assert families.factor_rank(context)
        assert "factor_score" in context.ind

    def test_score_not_computed_for_rejected_names(self):
        """没过门槛就不算分——算了也白算。"""
        context = ctx(rsi14=90.0)
        assert not families.factor_rank(context)
        assert "factor_score" not in context.ind

    def test_higher_momentum_scores_higher(self):
        strong, weak = ctx(momentum=0.20), ctx(momentum=0.02)
        families.factor_rank(strong)
        families.factor_rank(weak)
        assert strong.ind["factor_score"] > weak.ind["factor_score"]

    def test_distance_from_ma20_is_penalised(self):
        """离 MA20 越远越像追高，分数要被压下去。"""
        near = ctx(close=10.1, ma20=10.0)
        far = ctx(close=13.0, ma20=10.0)
        families.factor_rank(near)
        families.factor_rank(far)
        assert near.ind["factor_score"] > far.ind["factor_score"]

    def test_weights_are_configurable(self):
        heavy = ctx(momentum=0.10, cfg={"factor_weights": {"momentum": 10.0}})
        light = ctx(momentum=0.10, cfg={"factor_weights": {"momentum": 0.1}})
        families.factor_rank(heavy)
        families.factor_rank(light)
        assert heavy.ind["factor_score"] > light.ind["factor_score"]


class TestFamiliesAreActuallyDifferent:
    """对照实验的前提：同一只票在不同族下的结论应当**不全相同**。

    如果九个族对任何输入都给出一致答案，那对照就没有意义了——
    而这种退化不会有任何报错。
    """

    def test_a_pulling_back_strong_stock_splits_the_families(self):
        # 强势股回调中：RSI 超卖、未发生穿越、仍在 MA20 之上
        pullback = dict(rsi14=28.0, close=9.9, ma20=10.0, ma5=10.2,
                        cross_up_ma20=False, golden_cross=False, momentum=0.03)
        verdicts = {
            "mean_reversion": families.mean_reversion(ctx(**pullback)),
            "cross_up_ma20": families.cross_up_ma20(ctx(**pullback)),
            "ma5_cross_ma20": families.ma5_cross_ma20(ctx(**pullback)),
        }
        assert verdicts["mean_reversion"] is True
        assert verdicts["cross_up_ma20"] is False
        assert verdicts["ma5_cross_ma20"] is False
