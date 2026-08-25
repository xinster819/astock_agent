"""fees · A 股交易成本。

费率是账本里唯一"外部世界定死"的常量，单独成模块以便：
  - 纯函数、零依赖，可被 core 之外任何层安全引用；
  - 调费率时只有一处要改，不必在 broker 里翻找。

口径（券商常见档位，与真实成交单一致）：
  佣金   双边 万2.5，单笔最低 5 元
  印花税 卖出单边 千1
  过户费 双边 万0.1
"""
from __future__ import annotations

COMMISSION_RATE = 0.00025    # 佣金 万2.5
COMMISSION_MIN = 5.0         # 单笔最低 5 元
STAMP_TAX_RATE = 0.001       # 印花税 千1，仅卖出
TRANSFER_RATE = 0.00001      # 过户费 万0.1，双边
INIT_CASH = 1_000_000.0      # 默认初始资金 100 万

#: 买入时"每元成交额附带的比例费用"。用于反推最大可买数量——
#: 佣金保底 5 元不在其中，所以由它算出的上限偏乐观，调用方需再校验一次现金。
BUY_RATE_LOAD = COMMISSION_RATE + TRANSFER_RATE


def commission(amount: float) -> float:
    return max(amount * COMMISSION_RATE, COMMISSION_MIN)


def buy_fee(amount: float) -> float:
    """买入总费用：佣金 + 过户费（无印花税）。"""
    return round(commission(amount) + amount * TRANSFER_RATE, 2)


def sell_fee(amount: float) -> float:
    """卖出总费用：佣金 + 印花税 + 过户费。"""
    return round(commission(amount) + amount * STAMP_TAX_RATE + amount * TRANSFER_RATE, 2)


def max_affordable_qty(cash: float, price: float, lot: int = 100) -> int:
    """给定现金与价格，最多能买几股（向下取整到整手）。

    只扣比例费用，不含佣金保底 5 元，因此结果可能仍略微超出现金；
    调用方必须在下单前用 `buy_fee` 复核一次。这是刻意的：
    保底费是阶梯的，直接解析求逆会引入更难验证的分支。
    """
    if price <= 0 or cash <= 0:
        return 0
    raw = cash / (price * (1 + BUY_RATE_LOAD))
    return int(raw // lot) * lot
