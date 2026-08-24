"""strategy · 信号生成（纯计算，不做 IO 决策）。

⚠ 这里**刻意不做 re-export**。曾经这个 `__init__` 把 `signals` 的公开函数平铺到
包级，于是同一个函数有了两处绑定：生产代码 `from astock import strategy` 拿到的是
包属性，测试 `monkeypatch(signals, "generate_signals", …)` 改的是模块属性——
补丁打不中，测试**看起来在测护栏接线，实际上什么也没拦**。

统一约定：一律 `from astock.strategy import signals`，只有一处绑定。
"""
