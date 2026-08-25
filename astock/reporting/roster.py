"""roster · 报表侧的账户名册：13 个账户各自叫什么、账本在哪。

【替代了什么】
`dashboard.py` 和 `weekly.py` 各自硬编码了一份 13 行的账户表，
每行是 `(显示名, 描述, state路径, equity路径, trades路径)`。两份表带来两个问题：

  1. **路径重复**：13×3 = 39 个路径字符串写死两遍，而 `runtime.paths` 已经知道
     这些路径。账本布局一变，两张表都要手改。
  2. **名称与配置脱节**（已经发生）：实验组的名称和描述同时存在于
     `config/experiments/exp*.json` 和这两张硬编码表里。改配置报表不会跟着变——
     实测 exp9 配置里叫「多因子横截面排序」，两张表里都还写着「多因子排序」，
     描述则是各自截断改写过的旧副本。

     报表是对照实验的**结论出口**。结论上写着的策略名和实际在跑的策略名对不上，
     是比数字算错更难察觉的一类错误。

现在：**配置是名称的唯一权威**，路径来自 `AccountPaths`，名册只有这一份。
"""
from __future__ import annotations

from dataclasses import dataclass

from astock.core import experiments
from astock.runtime.paths import AGENT_GROUPS, CONTROL_GROUP, AccountPaths

#: A/B/C/D 组没有配置文件，名称在这里定义。exp* 一律以配置为准。
_GROUP_LABELS = {
    "A": ("纯规则对照", "基准对照组：纯规则自动交易，不加组合风控以保持可比性"),
    "B": ("Agent决策", "规则做护栏，agent 做最终买卖判断"),
    "C": ("多空辩论", "规则做护栏，多智能体多空辩论后做最终买卖判断"),
    "D": ("新闻情绪", "规则做护栏，结合新闻情绪面做最终买卖判断"),
}


@dataclass(frozen=True)
class ReportAccount:
    """报表眼中的一个账户。"""

    account: str            # "A" / "B" / "exp1" …，同时是互斥锁 key
    name: str               # 策略名，exp* 取自配置
    desc: str               # 一句话说明
    paths: AccountPaths

    @property
    def label(self) -> str:
        """报表里显示的完整标签，如 `exp4·金叉策略`、`A组·纯规则对照`。"""
        prefix = f"{self.account}组" if self.account in _GROUP_LABELS else self.account
        return f"{prefix}·{self.name}"

    @property
    def is_agent(self) -> bool:
        return self.account in AGENT_GROUPS

    @property
    def is_control(self) -> bool:
        return self.account == CONTROL_GROUP


def _for_group(group: str) -> ReportAccount:
    name, desc = _GROUP_LABELS[group]
    return ReportAccount(account=group, name=name, desc=desc,
                         paths=AccountPaths.for_group(group))


def _for_experiment(exp_id: str) -> ReportAccount:
    """实验组：名称与描述**以配置为准**，配置缺失时退回 id 本身。"""
    config = experiments.get_exp_config(exp_id) or {}
    return ReportAccount(
        account=exp_id,
        name=config.get("name", exp_id),
        desc=config.get("desc", ""),
        paths=AccountPaths.for_experiment(exp_id),
    )


def roster() -> list[ReportAccount]:
    """全部 13 个账户，顺序固定：A → exp1…exp9 → B/C/D。

    顺序即报表的行序，与 `paths.all_accounts()` 不同——那个按"账本布局"排，
    这个按"读报表的人想怎么看"排：先基线，再九组规则实验，最后三组 agent。
    """
    accounts = [_for_group(CONTROL_GROUP)]
    accounts += [_for_experiment(exp_id) for exp_id in experiments.EXPERIMENTS]
    accounts += [_for_group(g) for g in AGENT_GROUPS]
    return accounts


def by_account() -> dict[str, ReportAccount]:
    """按账户 id 索引，保持 roster() 的顺序。三处报表共用它来取名称。"""
    return {account.account: account for account in roster()}


def by_label() -> dict[str, ReportAccount]:
    """按显示标签索引。周报要用上周的标签去对本周的账户。"""
    return {account.label: account for account in roster()}
