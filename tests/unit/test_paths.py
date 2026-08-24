"""runtime.paths · 账本路径的单一事实源。

重构前这套逻辑在 18 个模块里各写了一遍 `os.path.dirname(__file__)`，
把**代码位置**当成了**数据位置**。本文件钉住三件事：

  1. 13 个账户的历史布局一字不改（迁移风险为零）
  2. 路径在**运行期**解析，同一进程内可以切换工作区
  3. AccountPaths 是不可变值对象，不带全局状态
"""
import dataclasses
import os

import pytest

from astock.runtime import paths
from astock.runtime.paths import AccountPaths


class TestHistoricalLayoutIsPreserved:
    """账本布局必须与重构前逐字节一致——线上有真实数据，不能让它们成为孤儿。"""

    def test_control_group_lives_at_workspace_root(self, isolated_env):
        a = AccountPaths.for_group("A")
        assert a.state == isolated_env / "state.json"
        assert a.trades == isolated_env / "trades.csv"
        assert a.equity == isolated_env / "equity.csv"

    @pytest.mark.parametrize("group", ["B", "C", "D"])
    def test_agent_groups_live_in_group_dirs(self, isolated_env, group):
        p = AccountPaths.for_group(group)
        assert p.state == isolated_env / f"group{group}" / "state.json"
        assert p.decision_input == isolated_env / f"group{group}" / "decision_input.json"

    @pytest.mark.parametrize("n", range(1, 10))
    def test_experiments_use_prefix_naming(self, isolated_env, n):
        p = AccountPaths.for_experiment(f"exp{n}")
        assert p.state == isolated_env / "experiments" / f"exp{n}_state.json"
        assert p.trades == isolated_env / "experiments" / f"exp{n}_trades.csv"

    def test_all_accounts_covers_exactly_thirteen(self):
        accounts = list(paths.all_accounts())
        assert len(accounts) == 13
        assert [a.account for a in accounts] == \
            ["A", "B", "C", "D"] + [f"exp{i}" for i in range(1, 10)]


class TestRuntimeResolution:
    """路径必须在**调用时**解析，不能在 import 时钉死。

    这正是 broker.py 当年做不到的事：模块级 STATE_PATH 在 import 期就定了，
    同一进程内碰不了第二个账户，测试必须改环境变量再 reload 模块，
    也因此这个仓库里唯一动钱的模块一直没有测试。
    """

    def test_workspace_follows_env_changes_within_one_process(self, monkeypatch, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv("ASTOCK_HOME", str(first))
        assert AccountPaths.for_group("B").state.parent.parent == first
        monkeypatch.setenv("ASTOCK_HOME", str(second))
        assert AccountPaths.for_group("B").state.parent.parent == second

    def test_two_accounts_coexist_in_one_process(self, isolated_env):
        """13 个账户可以同时打开——对照实验的报表就靠这个。"""
        opened = [AccountPaths.for_account(a) for a in ("A", "B", "exp1", "exp9")]
        assert len({str(p.state) for p in opened}) == 4

    def test_import_does_no_io(self, isolated_env):
        """解析路径不得创建任何目录。建目录必须是显式的 ensure_dirs()。"""
        AccountPaths.for_group("C")
        assert not (isolated_env / "groupC").exists()
        AccountPaths.for_group("C").ensure_dirs()
        assert (isolated_env / "groupC").is_dir()


class TestAccountResolution:

    def test_defaults_to_control_group(self, isolated_env, monkeypatch):
        monkeypatch.delenv("ASTOCK_GROUP", raising=False)
        assert AccountPaths.for_account().account == "A"

    def test_reads_group_from_env(self, isolated_env, monkeypatch):
        monkeypatch.setenv("ASTOCK_GROUP", "C")
        assert AccountPaths.for_account().account == "C"

    @pytest.mark.parametrize("raw,expected", [
        (" b ", "B"), ("d", "D"), ("EXP3", "exp3"), ("exp3", "exp3"),
    ])
    def test_normalizes_input(self, isolated_env, raw, expected):
        assert AccountPaths.for_account(raw).account == expected

    def test_empty_group_falls_back_to_control(self, isolated_env):
        assert AccountPaths.for_group("").account == "A"


class TestImmutability:

    def test_account_paths_is_frozen(self, isolated_env):
        p = AccountPaths.for_group("A")
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.account = "B"

    def test_archived_decision_is_timestamped(self, isolated_env):
        p = AccountPaths.for_group("B")
        assert p.archived_decision("20260825_143000").name == \
            "decision_output_20260825_143000.json"


class TestConfigRoot:

    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASTOCK_CONFIG", str(tmp_path))
        assert paths.config_root() == tmp_path

    def test_falls_back_to_repo_root_when_no_config_dir(self, monkeypatch):
        """尚未迁移的工作区要继续可用，不强迫一次性搬家。"""
        monkeypatch.delenv("ASTOCK_CONFIG", raising=False)
        assert paths.config_root().is_dir()

    def test_finds_experiment_configs(self, monkeypatch):
        monkeypatch.delenv("ASTOCK_CONFIG", raising=False)
        assert paths.experiment_config("exp1", "exp1_baseline.json").exists()


class TestRuntimeArtifacts:

    def test_all_runtime_paths_sit_under_workspace(self, isolated_env):
        for path in (paths.logs_dir(), paths.locks_dir(),
                     paths.spread_log(), paths.jitter_log()):
            assert str(path).startswith(str(isolated_env)), path

    def test_workspace_env_is_expanded(self, monkeypatch):
        monkeypatch.setenv("ASTOCK_HOME", "~/astock-test-home")
        assert "~" not in str(paths.workspace())
        assert str(paths.workspace()).startswith(os.path.expanduser("~"))
