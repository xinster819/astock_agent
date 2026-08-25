"""tests/conftest.py · 全局测试隔离与共享夹具。

【为什么必须有这一层】
重构前测试直接散在仓库根，靠 `python test_xxx.py` 逐个跑，**从不一起跑**。
于是没人发现它们共享着可变全局状态：

  - `source_health.QUOTES` 是模块级单例，一个测试把 eastmoney 熔断了，
    后面文件里的 `market.get_hist` 就会跳过东财走兜底 —— 断言随之失败。
  - `strategy._CACHE` 指标缓存跨用例存活。
  - 账本路径来自环境变量，测试之间会互相看见对方写的 state.json。

这些用例单跑全绿、合跑就红。这正是本项目最警惕的"静默失效"在测试侧的翻版：
**测试没在测你以为的东西。** 下面的 autouse 夹具把每个用例还原到干净起点。
"""
from __future__ import annotations

import os
import sys

import pytest

# 让 `import astock` 在未安装包时也能工作（CI 里走 `pip install -e .`，
# 本地开发直接 `pytest` 也应该能跑）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astock.core.account import Account
from astock.runtime import paths

_ASTOCK_ENV = ("ASTOCK_HOME", "ASTOCK_CONFIG", "ASTOCK_GROUP", "TZ")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """每个用例一个全新工作区，且不继承外部的 ASTOCK_* 环境变量。

    这条夹具是 `AccountPaths` 重构换来的：路径改成运行期解析之后，
    只要换个环境变量就能把整套账本重定向到 tmp_path。
    重构前 `broker.STATE_PATH` 在 import 期就钉死了，做不到这件事——
    这也是当年 broker 一直没有测试的直接原因。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in _ASTOCK_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASTOCK_HOME", str(workspace))
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    return workspace


@pytest.fixture(autouse=True)
def reset_global_singletons():
    """复位跨用例存活的可变全局：源熔断器 + 指标缓存。

    用例结束后也复位一次——避免"最后一个用例把状态留给下一个文件"。
    """
    from astock.data import source_health
    from astock.strategy import signals

    source_health.QUOTES.reset()
    signals.clear_indicator_cache()
    yield
    source_health.QUOTES.reset()
    signals.clear_indicator_cache()


# ---------------------------------------------------------------------------
# 共享夹具
# ---------------------------------------------------------------------------

@pytest.fixture
def account(isolated_env):
    """一个 100 万初始资金的干净账户（A 组），账本落在隔离工作区。"""
    return Account.open("A", init_cash=1_000_000.0)


@pytest.fixture
def make_quote():
    """构造行情快照。默认给出健康的可成交价与不触板的涨跌停。

        make_quote("600519", price=100.0)
        make_quote("600519", price=110.0, limit_up=110.0)   # 封涨停
    """
    def _make(code: str, price: float = 10.0, **overrides):
        quote = {
            "code": code,
            "name": overrides.pop("name", f"股票{code}"),
            "price": price,
            "limit_up": round(price * 1.1, 2),
            "limit_down": round(price * 0.9, 2),
        }
        quote.update(overrides)
        return quote
    return _make


@pytest.fixture
def workspace_paths(isolated_env):
    """隔离工作区里的 paths 模块（已确认指向 tmp）。"""
    assert paths.workspace() == isolated_env
    return paths
