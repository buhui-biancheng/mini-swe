# -*- coding: utf-8 -*-
"""E2E：调低阈值验证 ROLLBACK 真实执行（用户思路：放大信号验证回退路径）。

原理：impact_threshold 调到 -1 → 任何编辑的影响面都超限 → TEST 红色后必然触发
ROLLBACK → 断言：状态经过 rollback、文件恢复、权重回退真实执行。
验证通过即证明回退机制真实可用（不是死代码）。
"""
import os
import sys

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest
from types import SimpleNamespace

FAILURE_LOG = """_____________________________ test_add _____________________________
    def test_add():
>       assert add(1, 2) == 3
E       assert 1 == 3
bug.py:5: in test_add
    return a - b
"""


@pytest.fixture
def bug_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "bug.py").write_text(
        "def add(a, b):\n    return a - b  # BUG: 应该是 a + b\n", encoding="utf-8")
    (proj / "test_bug.py").write_text(
        "from bug import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    return proj


def _make_fsm_low_threshold(bug_project, monkeypatch, docker_seq, threshold=-1.0):
    """构造 FSM：impact_threshold 调低 → 回退必然触发。"""
    from swe_agent.fsm.agent_fsm import AgentFSM

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    seq = list(docker_seq)
    state = {"docker": 0}

    def fake_docker(*a, **k):
        code = seq[min(state["docker"], len(seq) - 1)]
        state["docker"] += 1
        if code == 0:
            return SimpleNamespace(exit_code=0, stdout="2 passed in 0.01s", stderr="")
        return SimpleNamespace(exit_code=1, stdout=FAILURE_LOG, stderr="")

    monkeypatch.setattr("swe_agent.sandbox.docker_runner.run_in_docker", fake_docker)

    bug_file = os.path.join(str(bug_project), "bug.py")
    fsm = AgentFSM(bug_file, "pytest", max_retries=1, mode="dp")
    fsm.agent_config.impact_threshold = threshold  # 调低阈值（用户思路）
    return fsm, state


def _mock_chat(fsm, captured=None):
    bucket = [] if captured is None else captured

    def fake_chat(messages, tools=None, tool_executor=None,
                  max_rounds=5, usage_callback=None,
                      thinking=False, reasoning_effort="high"):
        bucket.append(list(messages))
        if usage_callback:
            usage_callback({})
        return ("done", messages)

    fsm.client.chat_with_tools = fake_chat
    return bucket


class TestRollbackE2E:
    def test_low_threshold_triggers_real_rollback(self, bug_project, monkeypatch):
        """阈值 -1 → TEST 红色 → 影响面必然超限 → ROLLBACK 真实执行。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        import types

        fsm, _ = _make_fsm_low_threshold(
            bug_project, monkeypatch, docker_seq=[1], threshold=-1.0)
        _mock_chat(fsm)

        # 给初始权重（模拟历史数据）
        fsm._initial_weights = {"bug.py::add": {"success": 2, "fail": 0}}

        # 模拟编辑过 bug.py（快照/回退需要 _edited_ranges 非空）
        fsm._edited_ranges = [(os.path.join(str(bug_project), "bug.py"), 1, 2)]
        # 先保存初始快照（FSM 真实流程会做，这里直接补）
        fsm.checkpoint.save_initial(os.path.join(str(bug_project), "bug.py"))

        # 直达 test 状态（绕过 LLM 自动流转，只测回退机制本身）
        fsm.machine.set_state("test")
        fsm._log_state_enter("test", 0)
        # 触发 TEST 失败路径 → 影响面计算 → 超限 → ROLLBACK
        fsm._on_enter_test()

        # 断言：回退真实执行了
        assert fsm.rollback_count >= 1, f"应至少回退 1 次，实际 {fsm.rollback_count}"
        print(f"✅ ROLLBACK 真实执行: rollback_count={fsm.rollback_count}")

        # 断言：文件被恢复到初始快照（代码回退）
        content = (bug_project / "bug.py").read_text(encoding="utf-8")
        assert "a - b" in content, "文件应恢复到初始（BUG 还在）"
        print("✅ 文件回退到初始快照")

        # 断言：权重回退到初始
        w = fsm.graph_manager.get_weights_snapshot()
        # success 回退到初始(2)，fail 记录本次失败(1)（fail 是事实不回退）
        assert w.get("bug.py::add") == {"success": 2, "fail": 1}, f"权重应回退: {w}"
        print("✅ 权重回退到初始快照（图不被污染）")

        # 断言：失败计数 +1（缺陷3 联动）
        assert fsm.graph_manager.get_fail_count("bug.py::add") >= 1, "fail_count 应记录"
        print("✅ fail_count 已记录")

    def test_normal_threshold_does_not_rollback(self, bug_project, monkeypatch):
        """对照组：正常阈值（100）→ 小影响面不回退 → 继续修复路径。"""
        fsm, _ = _make_fsm_low_threshold(
            bug_project, monkeypatch, docker_seq=[0], threshold=100.0)
        _mock_chat(fsm)
        fsm.machine.set_state("test")
        fsm._log_state_enter("test", 0)
        fsm._on_enter_test()
        assert fsm.rollback_count == 0, f"正常阈值不应回退: {fsm.rollback_count}"
        print("✅ 对照：正常阈值下不回退（阈值调低才触发，验证信号有效）")