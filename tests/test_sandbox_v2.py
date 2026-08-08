# -*- coding: utf-8 -*-
"""Phase 6 测试（TDD 先行）：COW 隔离 / 沙盒 registry / FSM 集成 / 零污染。

核心断言：沙盒模式下 Agent 的一切操作（编辑/测试/建图）都在工作副本，
真实代码目录（bug 项目）零改动。
"""
import os
import sys
import shutil

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest


@pytest.fixture
def real_project(tmp_path):
    """真实代码目录（= 用户的项目，必须零污染）。"""
    proj = tmp_path / "real_proj"
    proj.mkdir()
    (proj / "bug.py").write_text(
        "def add(a, b):\n    return a - b  # BUG\n", encoding="utf-8")
    (proj / "test_bug.py").write_text(
        "from bug import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    (proj / "notes.txt").write_text("用户的重要笔记，绝不能被动", encoding="utf-8")
    return proj


class TestCowManager:
    def test_create_isolated_copy(self, real_project):
        from swe_agent.sandbox.cow_manager import CowManager
        cm = CowManager(str(real_project))
        ws = cm.create(task_id="t_cow_1")
        assert os.path.exists(os.path.join(ws, "bug.py")), "副本应有 bug.py"
        assert os.path.exists(os.path.join(ws, "notes.txt")), "副本应含所有文件"
        # 原件不变
        assert "a - b" in (real_project / "bug.py").read_text(encoding="utf-8")
        # 副本可改且不影响原件
        with open(os.path.join(ws, "bug.py"), "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a + b\n")
        assert "a + b" not in (real_project / "bug.py").read_text(encoding="utf-8")
        assert "a - b" in (real_project / "bug.py").read_text(encoding="utf-8")
        cm.cleanup(task_id="t_cow_1")
        assert not os.path.exists(ws), "清理后副本应删除"
        print("✅ COW 副本隔离：改副本不影响原件，清理正常")

    def test_exclude_big_dirs(self, real_project):
        from swe_agent.sandbox.cow_manager import CowManager
        # 真实目录里有 .git/venv/.graph 大目录
        for d in (".git", "venv", ".graph", "__pycache__"):
            (real_project / d).mkdir(exist_ok=True)
            (real_project / d / "junk.bin").write_bytes(b"x" * 100)
        cm = CowManager(str(real_project))
        ws = cm.create(task_id="t_cow_2")
        for d in (".git", "venv", ".graph", "__pycache__"):
            assert not os.path.exists(os.path.join(ws, d)), f"副本不应含 {d}"
        cm.cleanup(task_id="t_cow_2")
        print("✅ 副本排除大目录（.git/venv/.graph/__pycache__）")


class TestSandboxRegistry:
    def test_edit_goes_to_workspace_not_real(self, real_project):
        """沙盒模式下 edit_function 改副本，真实代码零污染。"""
        from swe_agent.sandbox.l1_sandbox import L1Sandbox
        from swe_agent.tools.registry import ToolRegistry
        import json

        sb = L1Sandbox(str(real_project))
        ws = sb.create(task_id="t_sb_1")
        reg = ToolRegistry(skeleton_text="", code_dir=ws)
        result = reg.execute("edit_function", {
            "file_path": "bug.py", "start_line": 2, "end_line": 2,
            "new_code": "    return a + b\n"})
        data = json.loads(result)
        assert "error" not in data, f"编辑失败: {data}"
        # 副本被改
        assert "a + b" in open(os.path.join(ws, "bug.py"), encoding="utf-8").read()
        # 真实代码零污染
        assert "a - b" in (real_project / "bug.py").read_text(encoding="utf-8")
        assert "a + b" not in (real_project / "bug.py").read_text(encoding="utf-8")
        sb.cleanup()
        print("✅ 沙盒 registry：编辑走副本，真实代码零污染")


class TestFsmSandbox:
    def test_fsm_sandbox_real_code_untouched(self, real_project, monkeypatch):
        """FSM sandbox 模式跑通（mock LLM + mock docker），真实代码零改动。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        from types import SimpleNamespace

        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        # mock docker：测试通过
        def fake_docker(*a, **k):
            return SimpleNamespace(exit_code=0, stdout="1 passed", stderr="")
        monkeypatch.setattr("swe_agent.sandbox.docker_runner.run_in_docker", fake_docker)

        fsm = AgentFSM(
            bug_file=str(real_project / "bug.py"),
            test_command="python3 -m pytest test_bug.py -q",
            max_retries=1, mode="dp", sandbox=True,
        )
        # mock chat：直接成功
        def fake_chat(messages, tools=None, tool_executor=None,
                      max_rounds=5, usage_callback=None,
                      thinking=False, reasoning_effort="high"):
            if usage_callback:
                usage_callback({})
            return ("done", messages)
        fsm.client.chat_with_tools = fake_chat

        fsm._on_enter_init()
        assert fsm.state == "success"

        # 核心断言：真实代码零污染
        real_content = (real_project / "bug.py").read_text(encoding="utf-8")
        assert "a - b" in real_content, "真实代码必须保持 BUG 原样"
        assert "a + b" not in real_content, "真实代码绝不能被修复"
        # notes.txt 也不被动
        assert "绝不能被动" in (real_project / "notes.txt").read_text(encoding="utf-8")
        # 工作副本存在过（沙盒内发生了一切）
        # FSM 结束后应清理（cleanup 在成功路径）
        print("✅ FSM sandbox：修复发生在副本，真实代码零改动")