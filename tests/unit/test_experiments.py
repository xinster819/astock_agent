"""core.experiments · 实验组配置注册表与一致性校验。

这个模块守着对照实验的前提：**九组之间的差异必须真的存在于运行时**。
如果某个参数看起来被改了、实际却不参与计算，两组跑的就是同一套逻辑，
得出的对照结论是假的——而且没有任何迹象能让人察觉。
"""
import json

import pytest

from astock.core import experiments


class TestRegistry:

    def test_registers_exactly_nine_experiments(self):
        assert len(experiments.EXPERIMENTS) == 9
        assert set(experiments.EXPERIMENTS) == {f"exp{i}" for i in range(1, 10)}

    @pytest.mark.parametrize("exp_id", [f"exp{i}" for i in range(1, 10)])
    def test_every_registered_config_exists_and_loads(self, exp_id):
        config = experiments.get_exp_config(exp_id)
        assert config is not None, f"{exp_id} 的配置文件缺失"
        assert config.get("name")

    def test_unknown_id_returns_none(self):
        assert experiments.get_exp_config("exp99") is None
        assert experiments.get_exp_config("A") is None

    def test_is_experiment(self):
        assert experiments.is_experiment("exp1")
        assert not experiments.is_experiment("A")


class TestSignalLogicIsDistinct:
    """九组的信号族必须两两不同，否则"对照"就名不副实。"""

    def test_signal_families_are_not_accidentally_identical(self):
        configs = {e: experiments.get_exp_config(e) for e in experiments.EXPERIMENTS}
        signatures = {}
        for exp_id, config in configs.items():
            key = (config.get("signal_logic"), config.get("momentum_threshold"),
                   config.get("stop_loss"), config.get("take_profit"))
            signatures.setdefault(key, []).append(exp_id)
        duplicates = {k: v for k, v in signatures.items() if len(v) > 1}
        assert not duplicates, f"以下实验组参数完全相同，对照无意义：{duplicates}"


class TestConfigValidation:
    """ma_slow 是配置里的冗余字段：读出来但不参与计算，改它不会有任何效果。

    九份配置目前恰好都与 signal_logic 自洽，所以这个陷阱一直没被踩到。
    与其留着，不如让矛盾直接报错——宁可开不了盘，也不能让配置默默失效。
    """

    def test_consistent_config_passes(self):
        config = {"signal_logic": "cross_up_ma20", "ma_slow": 20}
        assert experiments.validate_config("exp1", config) is config

    def test_contradiction_is_refused(self):
        with pytest.raises(experiments.ConfigError, match="不会有任何效果"):
            experiments.validate_config(
                "exp1", {"signal_logic": "cross_up_ma20", "ma_slow": 30})

    def test_error_names_both_sides(self):
        with pytest.raises(experiments.ConfigError) as exc:
            experiments.validate_config(
                "exp3", {"signal_logic": "cross_up_ma30", "ma_slow": 10})
        message = str(exc.value)
        assert "exp3" in message and "ma_slow=10" in message and "cross_up_ma30" in message

    def test_absent_ma_slow_is_fine(self):
        experiments.validate_config("exp5", {"signal_logic": "pure_momentum"})

    def test_logic_without_implied_ma_is_not_checked(self):
        """pure_momentum 之类不含慢线语义的信号族，不该被这条规则误伤。"""
        experiments.validate_config("exp5", {"signal_logic": "pure_momentum", "ma_slow": 99})

    def test_shipped_configs_all_pass(self):
        """出厂配置必须全部自洽——这条挂了说明有人改配置时没跑测试。"""
        for exp_id in experiments.EXPERIMENTS:
            experiments.get_exp_config(exp_id)

    def test_loading_a_contradictory_file_raises(self, isolated_env, monkeypatch, tmp_path):
        config_dir = tmp_path / "cfg" / "experiments"
        config_dir.mkdir(parents=True)
        (config_dir / "exp1_baseline.json").write_text(
            json.dumps({"name": "坏配置", "signal_logic": "cross_up_ma10", "ma_slow": 20}),
            encoding="utf-8")
        monkeypatch.setenv("ASTOCK_CONFIG", str(tmp_path / "cfg"))
        with pytest.raises(experiments.ConfigError):
            experiments.get_exp_config("exp1")


class TestListExperiments:

    def test_lists_all_nine_with_account_snapshot(self, isolated_env):
        listed = experiments.list_experiments()
        assert len(listed) == 9
        for item in listed:
            assert item["round"] == 0
            assert item["cash"] > 0
            assert item["total"] == pytest.approx(item["cash"])

    def test_uses_configured_initial_cash(self, isolated_env):
        listed = {item["id"]: item for item in experiments.list_experiments()}
        config = experiments.get_exp_config("exp1")
        expected = config.get("init_cash", config.get("cash"))
        if expected:
            assert listed["exp1"]["cash"] == pytest.approx(expected)
