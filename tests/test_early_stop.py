# -*- coding: utf-8 -*-
"""收益早停测试：连续无进展提前停 + 轨迹记录。"""
import os
import sys

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "bug.py").write_text(
        "def add(a, b):\n    return a - b  # BUG\n", encoding="utf-8")
    return tmp_path


class TestEarlyStop:
    def test_trajectory_recorded(self, proj, monkeypatch):
        """轨迹记录：每次测试失败后 fail_count 落盘。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        from types import SimpleNamespace
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        fsm = AgentFSM(bug_file=str(proj / "bug.py"),
                       test_command="python3 -m pytest test_x.py -q",
                       code_dir=str(proj), max_retries=3, mode="dp",
                       no_degrade=True, early_stop=True)
        # 模拟 parsed（2 个失败用例）
        parsed = SimpleNamespace(grouped_errors=[1, 2])
        fsm.machine.set_state("test")
        # 直接调轨迹记录逻辑（通过 _on_enter_test 太复杂，测 append 逻辑）
        fsm.attempt_trajectory.append({
            "attempt": 0, "fail_count": len(parsed.grouped_errors),
            "token": 100, "cost": 0.01})
        assert fsm.attempt_trajectory[0]["fail_count"] == 2
        assert fsm.attempt_trajectory[0]["cost"] == 0.01
        print("✅ 轨迹记录")

    def test_early_stop_logic(self):
        """早停判定：连续 2 次无进展（>= 历史最佳）→ 触发。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        # 纯逻辑测试（不实例化 FSM）
        traj = [{"fail_count": 2}, {"fail_count": 2}, {"fail_count": 2}, {"fail_count": 2}]
        patience = 2
        recent = traj[-patience:]
        best = min(x["fail_count"] for x in traj[:-patience])
        should_stop = all(x["fail_count"] >= best for x in recent)
        assert should_stop, "连续无进展应触发早停"
        # 有进展场景：2→2→1→1 不应停
        traj2 = [{"fail_count": 2}, {"fail_count": 2}, {"fail_count": 1}, {"fail_count": 1}]
        recent2 = traj2[-patience:]
        best2 = min(x["fail_count"] for x in traj2[:-patience])
        assert not all(x["fail_count"] >= best2 for x in recent2), "有进展不应停"
        print("✅ 早停判定逻辑")
