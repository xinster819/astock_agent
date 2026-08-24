"""market.get_quote · 三源交叉验证。

这是整个系统的**头号安全承诺**，README 里写在防御层第一行：

    三源交叉验证（东财/新浪/腾讯），≥2 源且极差 ≤0.5% 取中位数
    分歧则 price=0 拒单——脏价宁可不交易，绝不用错价成交

它此前**一个测试都没有**。而下游所有东西都无条件信任这里吐出的 `price`：
撮合规则拿它成交、策略拿它算指标、报表拿它估值。这一层判错，
系统会以一个错误的价格成交，且账本、闸门、报表全都表现正常。

判定规则（逐条对应下面的用例）：
  有效源 ≥2 且 极差/均值 ≤ DIVERGE_TOL  → 通过，取中位数
  有效源 ≥2 但偏差 > DIVERGE_TOL        → 分歧，price=0（下单层拒单）
  有效源 ==1                            → 降级放行，标 single_source
  有效源 ==0                            → price=0
"""
import pytest

from astock.data import market

CODE = "600519"


def source(price, *, name="贵州茅台", limit_up=0.0, limit_down=0.0,
           prev_close=0.0, error=None):
    if error:
        return {"error": error}
    return {"code": CODE, "name": name, "price": price, "limit_up": limit_up,
            "limit_down": limit_down, "prev_close": prev_close,
            "open": 0.0, "high": 0.0, "low": 0.0}


@pytest.fixture
def sources(monkeypatch):
    """注入三源返回值。用法：sources({"eastmoney": source(10.0), ...})"""
    from astock.data import quote_sources

    def install(payload):
        monkeypatch.setattr(quote_sources, "fetch_all", lambda code, **_k: payload)
        return market.get_quote(CODE)
    return install


class TestAgreement:
    """多源一致时取中位数——中位数抗单源异常，比均值稳。"""

    def test_three_sources_agreeing_pass(self, sources):
        quote = sources({"eastmoney": source(100.00),
                         "sina": source(100.10),
                         "tencent": source(100.05)})
        assert quote["price"] == 100.05, "三源应取中位数"
        assert quote["cross"].startswith("agree")
        assert "diverge" not in quote

    def test_two_sources_agreeing_pass(self, sources):
        quote = sources({"eastmoney": source(100.00), "sina": source(100.10)})
        assert quote["price"] > 0
        assert quote["cross"].startswith("agree")

    def test_median_ignores_one_outlier_within_tolerance(self, sources):
        """三源都在容差内时，中位数天然屏蔽掉偏离最大的那个。"""
        quote = sources({"eastmoney": source(100.00),
                         "sina": source(100.00),
                         "tencent": source(100.30)})
        assert quote["price"] == 100.00

    def test_cross_message_reports_the_spread(self, sources):
        """判定理由要可读——事后复盘时要能看出当时几源、偏差多少。"""
        quote = sources({"eastmoney": source(100.00), "sina": source(100.10)})
        assert "2源" in quote["cross"] and "%" in quote["cross"]


class TestDivergenceRefusesToPrice:
    """这是最重要的一组：分歧必须让 price 归零，而不是挑一个「看起来对的」。"""

    def test_divergent_sources_yield_zero_price(self, sources):
        quote = sources({"eastmoney": source(100.00), "sina": source(120.00)})
        assert quote["price"] == 0.0, "脏价宁可不交易"
        assert quote["cross"] == "diverge"

    def test_divergence_reason_names_all_source_prices(self, sources):
        """拒单必须说清楚：哪几个源、各报了什么价。"""
        quote = sources({"eastmoney": source(100.00), "sina": source(120.00)})
        assert "100.0" in quote["diverge"] and "120.0" in quote["diverge"]

    def test_boundary_just_inside_tolerance_passes(self, sources):
        """恰好在容差内应放行——阈值两侧的行为都要钉死。"""
        base = 100.0
        spread = market.DIVERGE_TOL * 0.9
        quote = sources({"eastmoney": source(base),
                         "sina": source(base * (1 + spread))})
        assert quote["price"] > 0

    def test_boundary_just_outside_tolerance_is_refused(self, sources):
        base = 100.0
        spread = market.DIVERGE_TOL * 2
        quote = sources({"eastmoney": source(base),
                         "sina": source(base * (1 + spread))})
        assert quote["price"] == 0.0

    def test_one_wild_source_poisons_the_whole_quote(self, sources):
        """两源一致、第三源离谱时也必须拒单。

        极差是拿最高价和最低价算的——只要有一个源疯了，这只票本轮就不该交易。
        用「多数源同意」去救它，等于给错价开了后门。
        """
        quote = sources({"eastmoney": source(100.00),
                         "sina": source(100.02),
                         "tencent": source(250.00)})
        assert quote["price"] == 0.0
        assert quote["cross"] == "diverge"


class TestDegradedAndUnavailable:

    def test_single_source_is_allowed_but_flagged(self, sources):
        """单源降级放行，但必须留痕——上层据此提高警惕。"""
        quote = sources({"eastmoney": source(100.00),
                         "sina": source(0, error="timeout"),
                         "tencent": source(0, error="timeout")})
        assert quote["price"] == 100.00
        assert quote["cross"] == "single_source(eastmoney)"

    def test_all_sources_failing_yields_zero(self, sources):
        quote = sources({"eastmoney": source(0, error="timeout"),
                         "sina": source(0, error="timeout"),
                         "tencent": source(0, error="timeout")})
        assert quote["price"] == 0.0
        assert quote["cross"] == "no_quote"

    def test_zero_price_counts_as_invalid_not_as_a_price(self, sources):
        """0 是「没有价」，不是「价格为零」。它绝不能参与交叉验证计数。"""
        quote = sources({"eastmoney": source(100.00), "sina": source(0.0),
                         "tencent": source(0.0)})
        assert quote["cross"] == "single_source(eastmoney)"

    def test_circuit_broken_source_still_counts_as_a_failure(self, sources):
        """熔断的源以 error 参与计数——熔断只省延迟，不改判定。

        否则「三源熔断了两个」会被误判成「单源一致」而放行。
        """
        quote = sources({"eastmoney": source(100.00),
                         "sina": source(0, error="circuit_open"),
                         "tencent": source(0, error="circuit_open")})
        assert quote["cross"].startswith("single_source")


class TestMetadata:
    """涨跌停等元数据是撮合规则的硬校验依据，不能因为交叉验证而丢失。"""

    def test_limit_prices_are_picked_from_the_first_source_that_has_them(self, sources):
        quote = sources({"eastmoney": source(100.0, limit_up=0.0, limit_down=0.0),
                         "sina": source(100.0, limit_up=110.0, limit_down=90.0)})
        assert quote["limit_up"] == 110.0
        assert quote["limit_down"] == 90.0

    def test_name_comes_from_the_first_healthy_source(self, sources):
        quote = sources({"eastmoney": source(100.0, error="timeout"),
                         "sina": source(100.0, name="贵州茅台"),
                         "tencent": source(100.0, name="贵州茅台")})
        assert quote["name"] == "贵州茅台"

    def test_raw_source_prices_are_preserved_for_audit(self, sources):
        """事后要能重放「当时三个源各报了什么」，否则分歧无从复盘。"""
        quote = sources({"eastmoney": source(100.00), "sina": source(120.00),
                         "tencent": source(0, error="timeout")})
        assert quote["sources"]["eastmoney"] == 100.00
        assert quote["sources"]["sina"] == 120.00
        assert quote["sources"]["tencent"] == "ERR"

    def test_every_quote_carries_an_exchange_timestamp(self, sources):
        quote = sources({"eastmoney": source(100.0), "sina": source(100.0)})
        assert quote["ts"], "取价时刻要留痕，价差校准与事后复盘都要用"


class TestDownstreamContract:
    """下游只认一件事：price>0 才可成交。这里把这个契约焊死。"""

    @pytest.mark.parametrize("payload,tradable", [
        ({"eastmoney": source(100.0), "sina": source(100.05)}, True),
        ({"eastmoney": source(100.0), "sina": source(130.0)}, False),
        ({"eastmoney": source(100.0)}, True),
        ({"eastmoney": source(0, error="x")}, False),
    ])
    def test_price_is_zero_exactly_when_the_quote_is_untrustworthy(
            self, sources, payload, tradable):
        from astock.core import rules

        quote = sources(payload)
        state = {"cash": 1_000_000.0, "init_cash": 1_000_000.0, "positions": {}}
        result = rules.buy(state, {**quote, "code": CODE}, 100)
        assert result.ok is tradable
