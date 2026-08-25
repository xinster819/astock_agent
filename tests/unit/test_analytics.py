"""reporting.analytics · 绩效统计与「能不能下结论」的判据。

这个项目的全部价值是对照实验。而实测数据长这样：

    exp5   42 轮   12 笔平仓   −12.48%
    D 组   20 轮    0 笔平仓   + 1.55%

一个普通排行榜会把 D 组放第一，读的人自然得出「新闻情绪策略胜出」——
而 D 组那 +1.55% 全是浮盈，一笔都没平过，观察期还只有 exp5 的一半。

所以本文件测的核心不是「收益率算得对不对」，而是**这套判据会不会让人过早下结论**。
"""
import pytest

from astock.reporting import analytics


def pnl_trades(*values):
    """构造成交流水。None 表示买入（无已实现盈亏）。"""
    return [{"pnl": v} for v in values]


def curve(*totals, start="2026-06-01 10:00:00"):
    day = 1
    points = []
    for total in totals:
        points.append({"t": f"2026-06-{day:02d} 10:00:00", "total": total, "ret": 0.0})
        day += 1
    return points


# =========================================================== 样本分级

class TestSampleTier:

    @pytest.mark.parametrize("n,key", [
        (0, "insufficient"), (5, "insufficient"), (9, "insufficient"),
        (10, "indicative"), (29, "indicative"),
        (30, "comparable"), (200, "comparable"),
    ])
    def test_tier_boundaries(self, n, key):
        assert analytics.sample_tier(n).key == key

    def test_only_the_top_tier_may_be_ranked(self):
        assert analytics.sample_tier(30).rank_eligible is True
        assert analytics.sample_tier(29).rank_eligible is False
        assert analytics.sample_tier(0).rank_eligible is False


# =========================================================== 单账户统计

class TestTradeStats:

    def test_counts_wins_losses_and_flats_separately(self):
        stats = analytics.trade_stats(pnl_trades(100, -50, 0, None, 200))
        assert (stats.wins, stats.losses, stats.flats) == (2, 1, 1)
        assert stats.closed == 4, "买入不计入平仓"

    def test_win_rate_excludes_flats(self):
        """平推既不算赢也不算输，不该稀释胜率。"""
        stats = analytics.trade_stats(pnl_trades(100, -50, 0, 0))
        assert stats.win_rate == 50.0

    def test_win_rate_is_none_without_decided_trades(self):
        """没有分出胜负时返回 None，不是 0%——0% 意味着「全亏」。"""
        assert analytics.trade_stats(pnl_trades(None, None)).win_rate is None

    def test_profit_factor(self):
        stats = analytics.trade_stats(pnl_trades(300, 100, -200))
        assert stats.profit_factor == 2.0

    def test_profit_factor_is_none_when_never_lost(self):
        """「从没亏过」在 3 笔样本下是运气，报成 ∞ 会让它看起来像圣杯。"""
        assert analytics.trade_stats(pnl_trades(100, 200, 300)).profit_factor is None

    def test_expectancy_is_the_mean_trade(self):
        assert analytics.trade_stats(pnl_trades(100, -50, 100)).expectancy == 50.0

    def test_empty_stats_are_all_none(self):
        stats = analytics.trade_stats([])
        assert stats.closed == 0
        assert stats.expectancy is None and stats.win_rate is None


class TestEdgeDetection:
    """全看板最该被看见的一行：这组的盈亏能不能与随机区分。"""

    def test_small_sample_is_never_judged(self):
        """回归：exp6 用 3 笔全赢就能让 |均值| > 2×SE。

        小样本下 t 分布尾巴很厚（df=2 的 95% 临界值是 4.30，不是 2），
        用固定的 2 去判等于把「3 笔全赢」认证成有优势。
        """
        stats = analytics.trade_stats(pnl_trades(100, 110, 105))
        assert stats.edge_is_detectable is None, "样本不足时必须不判，而不是判是"

    def test_consistent_losses_are_detectable(self):
        """稳定亏损也是一种可辨识——它同样是结论。"""
        stats = analytics.trade_stats(pnl_trades(
            -1000, -1100, -900, -1050, -980, -1020, -1150, -870, -1010, -990, -1080, -930))
        assert stats.edge_is_detectable is True
        assert stats.expectancy < 0

    def test_zero_variance_is_maximally_detectable(self):
        """每笔盈亏完全相同是最可辨识的情形，不是「看不出来」。

        早期实现把标准误差为 0 一并当作 None 返回，等于把
        「12 笔每笔都亏一千」判成了无法与随机区分。
        """
        stats = analytics.trade_stats(pnl_trades(*([-1000] * 12)))
        assert stats.std_error == 0
        assert stats.edge_is_detectable is True

    def test_exactly_zero_expectancy_is_not_detectable(self):
        """零方差且均值为零：确实没有优势，判否。"""
        stats = analytics.trade_stats(pnl_trades(*([0] * 12)))
        assert stats.edge_is_detectable is False

    def test_noisy_results_are_not_detectable(self):
        """大起大落但均值接近零：与随机不可区分。"""
        stats = analytics.trade_stats(pnl_trades(*([5000, -4800] * 8)))
        assert stats.edge_is_detectable is False

    def test_uses_t_critical_not_a_flat_two(self):
        """小样本的临界值必须显著大于 2，大样本才收敛到 1.96。"""
        assert analytics.t_critical_95(9) > 2.0
        assert analytics.t_critical_95(1000) == pytest.approx(1.96, abs=0.01)

    def test_t_critical_is_conservative_outside_the_table(self):
        """表外自由度向上取最接近的一档，宁可判得更严。"""
        assert analytics.t_critical_95(3) == analytics.t_critical_95(9)


# =========================================================== 权益曲线

class TestCurveStats:

    def test_max_drawdown_from_peak_to_trough(self):
        stats = analytics.curve_stats(curve(100, 120, 90, 110))
        assert stats.max_drawdown_pct == pytest.approx(25.0)   # 120 → 90

    def test_monotonic_rise_has_no_drawdown(self):
        assert analytics.curve_stats(curve(100, 110, 120)).max_drawdown_pct == 0.0

    def test_empty_curve_yields_none_not_zero(self):
        """没有观测 ≠ 零回撤。"""
        stats = analytics.curve_stats([])
        assert stats.max_drawdown_pct is None
        assert stats.observations == 0

    def test_counts_observations_and_span(self):
        stats = analytics.curve_stats(curve(100, 101, 102))
        assert stats.observations == 3
        assert stats.span_days == 2


# =========================================================== 可比性

def account(account_id, *, closed, rounds=40, dirty=False, stale=False, exists=True):
    return {
        "account": account_id, "exists": exists, "round": rounds,
        "closed_trades": closed, "dirty": dirty, "stale": stale,
        "tier": {"key": analytics.sample_tier(closed).key,
                 "label": analytics.sample_tier(closed).label,
                 "rank_eligible": analytics.sample_tier(closed).rank_eligible},
    }


class TestComparability:

    def test_healthy_matched_accounts_are_comparable(self):
        accounts = [account("exp1", closed=40), account("exp2", closed=45, rounds=42)]
        verdict = analytics.comparability(accounts)
        assert verdict.ok is True
        assert set(verdict.eligible) == {"exp1", "exp2"}

    def test_thin_samples_block_comparison(self):
        verdict = analytics.comparability(
            [account("exp1", closed=40), account("D", closed=0)])
        assert verdict.ok is False
        assert any("平仓笔数不足" in r for r in verdict.reasons)
        assert "D" in verdict.excluded

    def test_the_actual_production_shape_is_refused(self):
        """把线上实测数据喂进去：必须判为不可比。

        这是本模块存在的理由——13 组里没有一组的样本量支持任何结论。
        """
        real = [account("A", closed=15, rounds=103), account("exp5", closed=12, rounds=42),
                account("D", closed=0, rounds=20), account("exp8", closed=0, rounds=32)]
        verdict = analytics.comparability(real)
        assert verdict.ok is False
        assert verdict.eligible == []
        assert "没有账户满足比较条件" in verdict.headline

    def test_dirty_ledger_excludes_the_account(self):
        verdict = analytics.comparability(
            [account("exp1", closed=40), account("exp2", closed=40, dirty=True)])
        assert verdict.ok is False
        assert "exp2" in verdict.excluded
        assert any("对账" in r for r in verdict.reasons)

    def test_stale_data_excludes_the_account(self):
        verdict = analytics.comparability(
            [account("exp1", closed=40), account("exp2", closed=40, stale=True)])
        assert "exp2" in verdict.excluded
        assert any("过期" in r for r in verdict.reasons)

    def test_unequal_observation_windows_block_comparison(self):
        """轮次相差悬殊时，累计收益直接对比等于拿不同长度的观察期比大小。"""
        verdict = analytics.comparability(
            [account("A", closed=40, rounds=103), account("exp1", closed=40, rounds=31)])
        assert verdict.ok is False
        assert any("轮次" in r for r in verdict.reasons)

    def test_exclusion_reasons_are_attached_to_the_account(self):
        """排除必须写明理由，不能静默剔除。"""
        accounts = [account("exp1", closed=40), account("D", closed=0)]
        analytics.comparability(accounts)
        assert accounts[1]["exclusion_reasons"]

    def test_no_live_accounts(self):
        verdict = analytics.comparability([account("A", closed=0, exists=False)])
        assert verdict.ok is False


# =========================================================== 持仓重叠

class TestHoldingOverlap:

    def test_ranks_by_how_many_accounts_hold_it(self):
        accounts = [
            {"account": "A", "exists": True,
             "positions": [{"code": "600519", "name": "茅台", "mv": 100.0},
                           {"code": "000001", "name": "平安", "mv": 50.0}]},
            {"account": "B", "exists": True,
             "positions": [{"code": "600519", "name": "茅台", "mv": 200.0}]},
        ]
        overlap = analytics.holding_overlap(accounts)
        assert overlap[0]["code"] == "600519"
        assert overlap[0]["held_by"] == ["A", "B"]
        assert overlap[0]["total_mv"] == 300.0

    def test_skips_uninitialised_accounts(self):
        assert analytics.holding_overlap([{"account": "A", "exists": False}]) == []


class TestConcentration:

    def test_largest_position_share(self):
        acct = {"total": 1000.0, "positions": [{"mv": 300.0}, {"mv": 100.0}]}
        assert analytics.concentration(acct) == 30.0

    def test_none_when_flat_or_unknown(self):
        assert analytics.concentration({"total": 1000.0, "positions": []}) is None
        assert analytics.concentration({"total": 0, "positions": [{"mv": 1}]}) is None
