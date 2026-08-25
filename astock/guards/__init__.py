"""guards · 闸门层。所有闸门共享一条原则：宁可吵，也不沉默。

    risk       日内亏损/回撤/连亏冷却
    trade      账户互斥锁 + 冷却去抖（防幽灵成交）
    integrity  trades 重放对账、现金单调性、负现金
    freshness  权益新鲜度 + 引擎停摆检测
    regime     市场状态分类（live → last-known-good → 冷启动默认）
"""
