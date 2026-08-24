"""core · 账本与撮合规则——钱的唯一真相。

`fees` 定义费率，`ledger` 负责账本读写，`broker` 实现 A 股买卖硬校验。
本层只依赖 runtime，不碰网络。
"""
