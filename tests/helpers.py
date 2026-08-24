"""tests/helpers · 跨测试文件共享的构造器与打桩器。

重构前 `test_strategy_experiments.py` 直接 `from test_strategy_signals import
_StrategyStub`——靠把测试目录塞进 sys.path 才能工作，且让两个测试文件产生了
隐式依赖：改动 signals 的测试会连带弄坏 experiments 的测试。
共享物件放这里，两边都从 `tests.helpers` 取。
"""
from __future__ import annotations

from astock.strategy import signals as strategy


def make_indicators(code, ma5, ma20, momentum, golden_cross, close=10.0):
    """构造一个 `signals._indicators` 的返回值。"""
    return {
        "code": code, "close": close, "prev_close": close,
        "ma5": ma5, "ma10": ma5, "ma20": ma20, "prev_ma20": ma20,
        "momentum": momentum,
        "cross_up_ma20": False, "below_ma10": False,
        "golden_cross": golden_cross,   # MA5 上穿 MA20 的穿越事件
    }


def _memory_market_value(st, quotes):
    """纯内存估值，与 core.rules.market_value 同口径但不碰任何 IO。"""
    holdings = sum(
        float(p.get("qty", 0)) * float((quotes.get(code) or {}).get("price") or p.get("cost", 0))
        for code, p in st.get("positions", {}).items()
    )
    return holdings, float(st["cash"]) + holdings


class StrategyStub:
    """打桩 `signals._indicators` / `load_pool` 与 `rules.market_value`，纯内存。

    注意 market_value 必须打在 `core.rules` 上：`signals` 是在函数体内
    `from astock.core.rules import market_value` 动态取的，打在别处不生效。
    """

    def __init__(self, indicators_by_code, pool):
        self.map = indicators_by_code
        self.pool = pool
        self._orig = {}

    def __enter__(self):
        from astock.core import rules

        self._orig["_indicators"] = strategy._indicators
        self._orig["load_pool"] = strategy.load_pool
        self._orig["market_value"] = rules.market_value
        strategy._indicators = lambda code: self.map.get(code)
        strategy.load_pool = lambda: self.pool
        rules.market_value = _memory_market_value
        return self

    def __exit__(self, *exc):
        from astock.core import rules

        strategy._indicators = self._orig["_indicators"]
        strategy.load_pool = self._orig["load_pool"]
        rules.market_value = self._orig["market_value"]
