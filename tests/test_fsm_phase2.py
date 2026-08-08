"""Phase 2 FSM 集成测试：回滚/降级/取消/失败上下文注入/提示词分级/check 状态。

用 mock LLM（脚本化响应）+ mock Docker（脚本化 exit_code）跑完整 FSM 流程。
"""

import os
import json
from types import SimpleNamespace

import pytest

from swe_agent.fsm.agent_fsm import AgentFSM
from swe_agent.fsm.cancel_handler import CancelReason


FAILURE_LOG = (
    "============================= FAILURES =============================\n"
    "______________________________ test_add ______________________________\n"
    "\n"
    "    def test_add():\n"
    ">       assert add(1, 2) == 3\n"
    "E       assert 1 == 3\n"
    "\n"
    "/workspace/test_bug.py:5: in test_add\n"
    "    assert add(1, 2) == 3\n"
    "/workspace/bug.py:3: in add\n"
    "    return a - b\n"
    "E       AssertionError\n"
)


@pytest.fixture
def bug_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "bug.py").write_text(
        "def add(a, b):\n"
        "    return a - b  # Bug: 应为 a + b\n",
        encoding="utf-8",
    )
    (proj / "test_bug.py").write_text(
        "from bug import add\n"
        "\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return proj


def _make_fsm(bug_project, monkeypatch, mode="dp", docker_seq=None, max_retries=1):
    """构造 mock 好的 FSM。

    docker_seq: list[exit_code]，按调用顺序返回；默认 [0]（一次成功）。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    seq = list(docker_seq or [0])
    state = {"docker": 0}

    def fake_docker(*a, **k):
        code = seq[min(state["docker"], len(seq) - 1)]
        state["docker"] += 1
        if code == 0:
            return SimpleNamespace(exit_code=0, stdout="2 passed in 0.01s", stderr="")
        return SimpleNamespace(exit_code=1, stdout=FAILURE_LOG, stderr="")

    monkeypatch.setattr(
        "swe_agent.sandbox.docker_runner.run_in_docker", fake_docker
    )

    bug_file = os.path.join(str(bug_project), "bug.py")
    fsm = AgentFSM(bug_file, "pytest", max_retries=max_retries, mode=mode)
    return fsm, state


def _mock_chat(fsm, captured=None):
    """绑定一个收集消息的 mock chat（返回 ("done", messages)，不调用工具）。"""
    bucket = [] if captured is None else captured

    def fake_chat(messages, tools=None, tool_executor=None,
                  max_rounds=5, usage_callback=None,
                  thinking=False, reasoning_effort="high"):
        bucket.append(list(messages))
        if usage_callback:
            usage_callback({})  # 空 usage，不干扰预算测试
        return ("done", messages)

    fsm.client.chat_with_tools = fake_chat
    return bucket


class TestGoldenPath:
    def test_success(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[0])
        _mock_chat(fsm)
        fsm._on_enter_init()
        assert fsm.state == "success"

    def test_greedy_mode_success_switches_back_to_dp(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, mode="greedy", docker_seq=[0])
        _mock_chat(fsm)
        fsm._on_enter_init()
        assert fsm.state == "success"
        # Greedy 修复成功 → 回写图权重 + 切回 DP（模块 F）
        assert fsm.effective_mode == "dp"


class TestFailPathInjection:
    def test_fail_injects_grouped_errors(self, bug_project, monkeypatch):
        """test_fail → 注入结构化错误上下文 → 新一轮定位 → 成功。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[1, 0], max_retries=1)
        captured = _mock_chat(fsm)
        fsm._on_enter_init()
        assert fsm.state == "success"

        all_text = "\n".join(
            m.get("content", "") for msgs in captured for m in msgs
        )
        assert "【测试失败】" in all_text
        assert "AssertionError" in all_text
        assert "bug.py:3" in all_text
        assert "last_test.log" in all_text  # 日志落盘路径提示

    def test_full_log_written_to_disk(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[1, 0], max_retries=1)
        _mock_chat(fsm)
        fsm._on_enter_init()
        log_path = os.path.join(str(bug_project), ".graph", "last_test.log")
        assert os.path.exists(log_path)
        with open(log_path, "r", encoding="utf-8") as f:
            assert "AssertionError" in f.read()

    def test_system_prompt_rebuilt_per_round(self, bug_project, monkeypatch):
        """提示词分级：每轮 system 重建，失败后注入错误定位协议。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[1, 0], max_retries=1)
        captured = _mock_chat(fsm)
        fsm._on_enter_init()
        assert len(captured) >= 2
        for msgs in captured:
            assert msgs[0]["role"] == "system"
            assert "你是一个专业的代码修复助手" in msgs[0]["content"]
        # 失败后新一轮 system 注入错误定位协议
        assert "错误定位协议" in captured[-1][0]["content"]
        # system 不进 conversation 历史（重建而非追加）
        for msgs in captured:
            assert all(m["role"] != "system" for m in msgs[1:])


class TestRollback:
    def test_impact_circuit_breaker_restores_snapshot(self, bug_project, monkeypatch):
        """影响面 ≥ 阈值 → ROLLBACK：恢复初始快照 + 重新规划。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[1, 0], max_retries=1)
        fsm.agent_config.impact_threshold = 0  # 任何影响面都熔断
        _mock_chat(fsm)

        bug_file = os.path.join(str(bug_project), "bug.py")
        fsm.checkpoint.save_initial(bug_file)
        fsm._edited_ranges = [(bug_file, 1, 2)]
        with open(bug_file, "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a * b\n")  # 模拟编辑

        fsm.machine.set_state("test")
        fsm._on_enter_test()

        assert fsm.state == "success"
        # 成功路径计数器清零（计划：test_pass 重置）
        assert fsm.rollback_count == 0
        with open(bug_file, encoding="utf-8") as f:
            assert "return a - b" in f.read()  # 已恢复初始快照

    def test_check_fail_limit_triggers_rollback(self, bug_project, monkeypatch):
        """连续语法失败 ≥ 阈值 → ROLLBACK（check_exhausted）。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[0], max_retries=1)
        fsm.agent_config.check_fail_limit = 2
        _mock_chat(fsm)

        bug_file = os.path.join(str(bug_project), "bug.py")
        fsm.checkpoint.save_initial(bug_file)
        with open(bug_file, "w", encoding="utf-8") as f:
            f.write("def broken(:\n    pass\n")  # 语法错误

        fsm.machine.set_state("check")
        fsm._on_enter_check()

        assert fsm.state == "success"  # 回滚后恢复合法代码，重试成功
        # 成功路径计数器清零
        assert fsm.rollback_count == 0
        assert fsm._check_fail_count == 0  # check_pass 后清零

    def test_rollback_degrades_dp_to_greedy(self, bug_project, monkeypatch):
        """DP 反复失效（回滚超限）→ 降级 Greedy → 成功回写 → 切回 DP。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[0], max_retries=1)
        fsm.agent_config.rollback_limit = 1  # 1 次回滚后触发降级
        _mock_chat(fsm)

        bug_file = os.path.join(str(bug_project), "bug.py")
        fsm.checkpoint.save_initial(bug_file)
        fsm.machine.set_state("rollback")
        fsm._on_enter_rollback()

        assert fsm.state == "success"
        assert fsm.effective_mode == "dp"  # greedy 成功后切回 dp

    def test_greedy_still_failing_goes_to_fail(self, bug_project, monkeypatch):
        """已是 Greedy 仍回滚超限 → FAIL（交给人类）。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, mode="greedy", docker_seq=[0])
        fsm.agent_config.rollback_limit = 1
        bug_file = os.path.join(str(bug_project), "bug.py")
        fsm.checkpoint.save_initial(bug_file)
        fsm.machine.set_state("rollback")
        fsm._on_enter_rollback()
        assert fsm.state == "fail"


class TestCancel:
    def test_cancel_restores_snapshot(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch)
        bug_file = os.path.join(str(bug_project), "bug.py")
        fsm.checkpoint.save_initial(bug_file)
        with open(bug_file, "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a * b\n")

        fsm.cancel(reason=CancelReason.USER)

        assert fsm.state == "fail"
        assert fsm._cancel_reason == CancelReason.USER
        with open(bug_file, encoding="utf-8") as f:
            assert "return a - b" in f.read()

    def test_api_error_cancels(self, bug_project, monkeypatch):
        """LLM API 持续失败 → AgentAPIError → 取消事件。"""
        from swe_agent.llm.client import AgentAPIError

        fsm, _ = _make_fsm(bug_project, monkeypatch)

        def boom(messages, tools=None, tool_executor=None,
                 max_rounds=5, usage_callback=None,
                       thinking=False, reasoning_effort="high"):
            raise AgentAPIError("持续失败")

        fsm.client.chat_with_tools = boom
        fsm.machine.set_state("locate")
        fsm._on_enter_locate()
        assert fsm.state == "fail"
        assert fsm._cancel_reason == CancelReason.API_ERROR


class TestTokenBudget:
    def test_budget_exceeded_degrades_dp_to_greedy(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[0])
        fsm.token_budget.limit = 10
        fsm.token_budget.total = 100  # 已超限
        captured = _mock_chat(fsm)

        fsm.machine.set_state("locate")
        fsm._on_enter_locate()

        # 降级后 system 注入探索模式
        sys_text = captured[0][0]["content"]
        assert "探索模式" in sys_text
        assert fsm.state == "success"

    def test_budget_exceeded_mid_call_degrades(self, bug_project, monkeypatch):
        """调用中途超限（入口未超限，usage 回调触发）→ DP 降级 Greedy。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[0])
        fsm.token_budget.limit = 100
        fsm.token_budget.total = 95  # 入口未超限

        def chat_with_big_usage(messages, tools=None, tool_executor=None,
                                max_rounds=5, usage_callback=None,
                       thinking=False, reasoning_effort="high"):
            if usage_callback:
                usage_callback({"total_tokens": 20})  # 95+20=115 > 100 → 中途超限
            return ("done", messages)

        fsm.client.chat_with_tools = chat_with_big_usage
        fsm.machine.set_state("locate")
        fsm._on_enter_locate()

        # 中途超限 → 降级 greedy → 清空上下文 → 继续走流程 → 成功
        assert fsm.effective_mode == "dp"  # 成功后切回
        assert fsm.state == "success"

    def test_budget_exceeded_mid_call_in_greedy_cancels(self, bug_project, monkeypatch):
        """Greedy 模式调用中途超限 → 熔断。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, mode="greedy", docker_seq=[0])
        fsm.token_budget.limit = 100
        fsm.token_budget.total = 95

        def chat_with_big_usage(messages, tools=None, tool_executor=None,
                                max_rounds=5, usage_callback=None,
                       thinking=False, reasoning_effort="high"):
            if usage_callback:
                usage_callback({"total_tokens": 20})
            return ("done", messages)

        fsm.client.chat_with_tools = chat_with_big_usage
        fsm.machine.set_state("locate")
        fsm._on_enter_locate()
        assert fsm.state == "fail"
        assert fsm._cancel_reason == CancelReason.BUDGET_EXCEEDED

    def test_budget_exceeded_in_greedy_cancels(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, mode="greedy")
        fsm.token_budget.limit = 10
        fsm.token_budget.total = 100
        fsm.machine.set_state("locate")
        fsm._on_enter_locate()
        assert fsm.state == "fail"
        assert fsm._cancel_reason == CancelReason.BUDGET_EXCEEDED


class TestFailPathImpactSorting:
    """A1+A2：失败上下文按影响面排序 + 新起点图上下文注入。"""

    def test_failure_context_sorted_by_impact(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[1, 0], max_retries=1)
        captured = _mock_chat(fsm)
        fsm._on_enter_init()

        all_text = "\n".join(
            m.get("content", "") for msgs in captured for m in msgs
        )
        # 影响面排序标注 + 新起点图上下文注入
        assert "已按图影响面排序" in all_text
        assert "影响面=" in all_text
        assert "新起点图上下文" in all_text
        assert "bug.py::add" in all_text  # 新起点邻接包含目标节点

    def test_failure_context_marks_top_impact(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[1, 0], max_retries=1)
        captured = _mock_chat(fsm)
        fsm._on_enter_init()

        all_text = "\n".join(
            m.get("content", "") for msgs in captured for m in msgs
        )
        assert "优先定位" in all_text


class TestSignatureChangeCheck:
    """A3：签名变更调用方适配检查。"""

    def test_signature_change_notes_callers(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[1, 0], max_retries=1)
        fsm.agent_config.impact_threshold = 0  # 熔断回滚路径，检查 notes 是否注入
        captured = _mock_chat(fsm)

        bug_file = os.path.join(str(bug_project), "bug.py")
        fsm.checkpoint.save_initial(bug_file)
        # 编辑范围触及 add 的定义行（第 1 行）→ 签名变更
        fsm._edited_ranges = [(bug_file, 1, 1)]
        fsm.machine.set_state("check")
        fsm._on_enter_check()

        # 签名变更提醒注入失败上下文，随下一轮 chat 可见
        all_text = "\n".join(
            m.get("content", "") for msgs in captured for m in msgs
        )
        assert "签名变更提醒" in all_text
        assert "调用方" in all_text
        assert "test_bug.py::test_add" in all_text

    def test_body_edit_no_signature_note(self, bug_project, monkeypatch):
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[0])
        bug_file = os.path.join(str(bug_project), "bug.py")
        # 编辑第 2 行（函数体），不触及定义行第 1 行
        fsm._edited_ranges = [(bug_file, 2, 2)]
        fsm.machine.set_state("check")
        fsm._on_enter_check()
        assert fsm._signature_notes == []


class TestCheckState:
    def test_valid_code_passes_check_to_test(self, bug_project, monkeypatch):
        fsm, state = _make_fsm(bug_project, monkeypatch, docker_seq=[0])
        fsm.machine.set_state("check")
        fsm._on_enter_check()
        assert fsm.state == "success"  # check_pass → test（docker 0）

    def test_syntax_error_goes_back_to_patch(self, bug_project, monkeypatch):
        """语法错误 → check_fail → patch 重新生成修复 → mock LLM 修好后成功。"""
        fsm, _ = _make_fsm(bug_project, monkeypatch, docker_seq=[0])
        bug_file = os.path.join(str(bug_project), "bug.py")
        with open(bug_file, "w", encoding="utf-8") as f:
            f.write("def broken(:\n    pass\n")

        # mock LLM：第一轮（patch 重修复）把语法修好
        def fixing_chat(messages, tools=None, tool_executor=None,
                        max_rounds=5, usage_callback=None,
                       thinking=False, reasoning_effort="high"):
            bug_file = os.path.join(str(bug_project), "bug.py")
            with open(bug_file, "w", encoding="utf-8") as f:
                f.write("def broken():\n    pass\n")
            return ("fixed", messages)

        fsm.client.chat_with_tools = fixing_chat

        fsm.machine.set_state("check")
        fsm._on_enter_check()

        # 语法错误 → check_fail → patch（重新生成修复）→ 修好后 check → test → 成功
        assert fsm.state == "success"
