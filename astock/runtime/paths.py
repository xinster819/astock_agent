"""paths · 账本路径与工作区的单一事实源。

【为什么有这个模块】
重构前，18 个模块各自写了一遍 `os.path.dirname(__file__)` 来推算仓库根，
再各自拼出 state.json / trades.csv / group<X>/ / experiments/ 的位置。
后果有三：

  1. **代码位置 = 数据位置**：源码一旦挪窝（分包、装进 site-packages），
     账本路径集体失效。这不是假设，本次分包就一次性打断了全部 18 处。
  2. **导入期副作用**：`broker.py` 在 import 时就读 `ASTOCK_GROUP` 并 `makedirs`，
     模块级常量 STATE_PATH 从此钉死。同一进程内无法同时操作两个账户，
     测试必须改环境变量再 reload 模块，`run_exp` 因此没法复用 broker 的读写，
     只好在 `exp_manager` 里把账本 IO 又抄了一遍。
  3. **配置与数据混居**：`experiments/` 下同时躺着 exp1_baseline.json（配置，入库）
     和 exp1_state.json（账本，不入库），.gitignore 只能靠文件名模式勉强区分。

本模块把"账本在哪"变成一次显式解析，产出不可变的 `AccountPaths` 值对象。
调用方拿到的是数据，不是全局状态——这是消除上述三个问题的共同前提。

【目录约定】
    $ASTOCK_HOME/                 运行数据根（默认=仓库根，可用环境变量迁移）
        state.json trades.csv equity.csv        A 组（对照基线，历史布局，保持不变）
        groupB/ groupC/ groupD/                 Agent 决策组，各自独立账本
        experiments/exp<N>_state.json …         规则实验组，前缀命名（历史布局）
        logs/  .locks/  spread_log.csv          运行期产物

    $ASTOCK_CONFIG/               配置根（默认=仓库根/config，回退到仓库根）
        sample_pool.json                        价差采样池
        watchlist.json                          自选池
        experiments/exp<N>_*.json               实验组参数

配置根与数据根分离，是为了让"入库的配置"和"不入库的账本"物理隔开，
.gitignore 不必再靠文件名模式猜。
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "ASTOCK_HOME"
ENV_CONFIG = "ASTOCK_CONFIG"
ENV_GROUP = "ASTOCK_GROUP"

#: A 组=对照基线，账本直接落在工作区根（保持历史布局，迁移风险为零）。
CONTROL_GROUP = "A"
#: Agent 决策组，账本落 group<X>/。
AGENT_GROUPS = ("B", "C", "D")
#: 规则实验组数量，账本落 experiments/exp<N>_*。
EXPERIMENT_COUNT = 9

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent   # …/astock
_REPO_ROOT = _PACKAGE_ROOT.parent                        # …/astock_agent


def repo_root() -> Path:
    """源码仓库根。仅用于推导默认值，业务代码不要直接用它拼账本路径。"""
    return _REPO_ROOT


def workspace() -> Path:
    """运行数据根目录。

    每次调用都重新读环境变量——测试要能在同一进程里切换工作区，
    这正是重构前 broker 模块级常量做不到的事。
    """
    override = os.environ.get(ENV_HOME, "").strip()
    return Path(override).expanduser().resolve() if override else _REPO_ROOT


def config_root() -> Path:
    """配置根目录。

    解析顺序：$ASTOCK_CONFIG → 仓库根/config → 仓库根（历史布局兜底）。
    最后一级兜底让尚未迁移的工作区继续可用，不强迫一次性搬家。
    """
    override = os.environ.get(ENV_CONFIG, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    candidate = _REPO_ROOT / "config"
    return candidate if candidate.is_dir() else _REPO_ROOT


def logs_dir() -> Path:
    return workspace() / "logs"


def locks_dir() -> Path:
    return workspace() / ".locks"


def spread_log() -> Path:
    """多源价差审计日志（体积大、可重生成，不入库）。"""
    return workspace() / "spread_log.csv"


def jitter_log() -> Path:
    """调度抖动日志：记录计划延时与实际开跑时刻，用于判定进程是否被超时截断。"""
    return workspace() / "jitter_log.csv"


def sample_pool() -> Path:
    """价差校准采样池（沪深300），配置文件。"""
    return _find_config("sample_pool.json")


def watchlist() -> Path:
    """自选股池，配置文件。缺失时由 strategy 退回内置默认池。"""
    return _find_config("watchlist.json")


def experiment_config(exp_id: str, filename: str) -> Path:
    """实验组参数文件。先找 config/experiments/，再回退历史的 experiments/。"""
    return _find_config(filename, subdir="experiments")


def _find_config(filename: str, subdir: str = "") -> Path:
    """在配置根下定位配置文件，找不到就回退到历史位置。

    返回的路径**不保证存在**——是否存在由调用方判断，本模块不做 IO 决策。
    """
    root = config_root()
    primary = root / subdir / filename if subdir else root / filename
    if primary.exists():
        return primary
    legacy = _REPO_ROOT / subdir / filename if subdir else _REPO_ROOT / filename
    return legacy if legacy.exists() else primary


# ---------------------------------------------------------------------------
# 账户路径
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountPaths:
    """一个账户的全部落盘位置。不可变值对象，可自由传递、无全局状态。

    `account` 是账户标识（"A"/"B"/"C"/"D"/"exp1"…），同时用作互斥锁的 key。
    """

    account: str
    state: Path
    trades: Path
    equity: Path
    root: Path

    # ---- Agent 决策组专用（规则组不产生这些文件）----
    @property
    def decision_input(self) -> Path:
        return self.root / "decision_input.json"

    @property
    def decision_output(self) -> Path:
        return self.root / "decision_output.json"

    @property
    def decision_log(self) -> Path:
        return self.root / "decision_log.csv"

    def archived_decision(self, stamp: str) -> Path:
        """归档一次已执行的决策，文件名带时间戳，便于事后重放核对。"""
        return self.root / f"decision_output_{stamp}.json"

    def ensure_dirs(self) -> AccountPaths:
        """按需建目录。**显式调用**——绝不在 import 期做 IO。"""
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def for_account(cls, account: str | None = None) -> AccountPaths:
        """按账户标识解析路径。account 省略时读 $ASTOCK_GROUP（默认 A 组）。"""
        if account is None:
            account = os.environ.get(ENV_GROUP, CONTROL_GROUP)
        account = str(account).strip()
        if account.lower().startswith("exp"):
            return cls.for_experiment(account.lower())
        return cls.for_group(account.upper() or CONTROL_GROUP)

    @classmethod
    def for_group(cls, group: str) -> AccountPaths:
        """A/B/C/D 组。A 组落工作区根，其余落 group<X>/。"""
        group = group.strip().upper() or CONTROL_GROUP
        base = workspace()
        root = base if group == CONTROL_GROUP else base / f"group{group}"
        return cls(
            account=group,
            root=root,
            state=root / "state.json",
            trades=root / "trades.csv",
            equity=root / "equity.csv",
        )

    @classmethod
    def for_experiment(cls, exp_id: str) -> AccountPaths:
        """exp1…exp9。同一目录下靠 `exp<N>_` 前缀区分（历史布局，保持不变）。"""
        exp_id = exp_id.strip().lower()
        root = workspace() / "experiments"
        return cls(
            account=exp_id,
            root=root,
            state=root / f"{exp_id}_state.json",
            trades=root / f"{exp_id}_trades.csv",
            equity=root / f"{exp_id}_equity.csv",
        )


def all_accounts() -> Iterator[AccountPaths]:
    """遍历全部 13 个账户：A + B/C/D + exp1…exp9。报表与对账用。"""
    yield AccountPaths.for_group(CONTROL_GROUP)
    for g in AGENT_GROUPS:
        yield AccountPaths.for_group(g)
    for i in range(1, EXPERIMENT_COUNT + 1):
        yield AccountPaths.for_experiment(f"exp{i}")
