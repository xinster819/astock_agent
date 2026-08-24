"""reporting.roster · 报表账户名册。

重构前 dashboard.py 和 weekly.py 各自硬编码了一份 13 行的账户表，
每行含显示名、描述和三个账本路径。两张表带来两个问题，本文件把它们都钉住：

  1. 39 个路径字符串写死两遍，而 runtime.paths 已经知道这些路径
  2. **实验组名称同时存在于配置和硬编码表里**，改配置报表不会跟着变——
     实测 exp9 配置里叫「多因子横截面排序」，两张表里都还写着「多因子排序」

报表是对照实验的**结论出口**。结论上写着的策略名和实际在跑的策略名对不上，
比数字算错更难察觉。
"""
import pytest

from astock.core import experiments
from astock.reporting import roster
from astock.runtime.paths import AccountPaths


class TestComposition:

    def test_covers_all_thirteen_accounts(self, isolated_env):
        assert len(roster.roster()) == 13

    def test_order_is_baseline_then_experiments_then_agents(self, isolated_env):
        """行序即报表的阅读顺序：先基线，再九组规则实验，最后三组 agent。"""
        ids = [a.account for a in roster.roster()]
        assert ids == ["A"] + [f"exp{i}" for i in range(1, 10)] + ["B", "C", "D"]

    def test_no_duplicate_accounts(self, isolated_env):
        ids = [a.account for a in roster.roster()]
        assert len(set(ids)) == len(ids)

    def test_every_account_has_a_label_and_paths(self, isolated_env):
        for account in roster.roster():
            assert account.label
            assert isinstance(account.paths, AccountPaths)
            assert account.paths.state.name.endswith(".json")


class TestConfigIsAuthoritative:
    """实验组的名称与描述以配置为准，不再有第二份副本。"""

    @pytest.mark.parametrize("exp_id", [f"exp{i}" for i in range(1, 10)])
    def test_experiment_name_matches_its_config(self, isolated_env, exp_id):
        config = experiments.get_exp_config(exp_id)
        entry = next(a for a in roster.roster() if a.account == exp_id)
        assert entry.name == config["name"]
        assert entry.desc == config["desc"]

    def test_renaming_a_strategy_in_config_flows_through(self, isolated_env, monkeypatch):
        """这正是重构前做不到的事——改配置，报表纹丝不动。"""
        real = experiments.get_exp_config

        def renamed(exp_id):
            config = dict(real(exp_id) or {})
            if exp_id == "exp1":
                config["name"] = "改过名的策略"
            return config or None

        monkeypatch.setattr(roster.experiments, "get_exp_config", renamed)
        entry = next(a for a in roster.roster() if a.account == "exp1")
        assert entry.label == "exp1·改过名的策略"

    def test_missing_config_degrades_to_the_id(self, isolated_env, monkeypatch):
        """配置文件缺失不该让整份报表崩掉。"""
        monkeypatch.setattr(roster.experiments, "get_exp_config", lambda _e: None)
        entry = next(a for a in roster.roster() if a.account == "exp5")
        assert entry.name == "exp5"
        assert entry.label == "exp5·exp5"


class TestLabels:

    def test_group_labels_carry_the_group_suffix(self, isolated_env):
        labels = {a.account: a.label for a in roster.roster()}
        assert labels["A"] == "A组·纯规则对照"
        assert labels["B"] == "B组·Agent决策"

    def test_experiment_labels_use_the_bare_id(self, isolated_env):
        labels = {a.account: a.label for a in roster.roster()}
        assert labels["exp4"].startswith("exp4·")
        assert "组" not in labels["exp4"].split("·")[0]

    def test_labels_are_unique(self, isolated_env):
        """周报靠标签在跨周文件之间对齐账户，重名会让数据串组。"""
        labels = [a.label for a in roster.roster()]
        assert len(set(labels)) == len(labels)

    def test_by_label_index(self, isolated_env):
        index = roster.by_label()
        assert len(index) == 13
        assert index["A组·纯规则对照"].account == "A"


class TestPathsComeFromRuntime:
    """路径不再是写死的字符串——换个工作区就该整体跟着走。"""

    def test_paths_follow_the_workspace(self, isolated_env):
        for account in roster.roster():
            assert str(account.paths.state).startswith(str(isolated_env))

    def test_control_group_lives_at_the_workspace_root(self, isolated_env):
        control = next(a for a in roster.roster() if a.is_control)
        assert control.paths.state == isolated_env / "state.json"

    def test_agent_groups_are_flagged(self, isolated_env):
        agents = [a.account for a in roster.roster() if a.is_agent]
        assert agents == ["B", "C", "D"]

    def test_experiments_are_neither_control_nor_agent(self, isolated_env):
        exp = next(a for a in roster.roster() if a.account == "exp1")
        assert not exp.is_control and not exp.is_agent
