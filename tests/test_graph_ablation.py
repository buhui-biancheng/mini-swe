# -*- coding: utf-8 -*-
"""消融验证测试：graph_enabled=False → 图上下文/骨架/L-1 全空。"""
import os
import sys

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "bug.py").write_text(
        "def add(a, b):\n    return a - b  # BUG\n", encoding="utf-8")
    return tmp_path


class TestGraphAblation:
    def test_graph_disabled_empties_all_graph_info(self, proj, monkeypatch):
        """dp-无图：_graph_context_text / _file_level_prior_text / skeleton 全空。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

        fsm = AgentFSM(bug_file=str(proj / "bug.py"),
                       test_command="python3 -m pytest test_bug.py -q",
                       code_dir=str(proj), max_retries=1, mode="dp",
                       no_degrade=True, graph_enabled=False)

        assert fsm._graph_context_text() == "", "图上下文应为空"
        assert fsm._file_level_prior_text() == "", "L-1 文件级先验应为空"
        assert fsm.skeleton_text == "", "骨架应为空"
        print("✅ dp-无图：图上下文/L-1/骨架全空")

    def test_graph_enabled_has_info(self, proj, monkeypatch):
        """dp：图信息正常注入。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        fsm = AgentFSM(bug_file=str(proj / "bug.py"),
                       test_command="python3 -m pytest test_bug.py -q",
                       code_dir=str(proj), max_retries=1, mode="dp",
                       no_degrade=True, graph_enabled=True)
        assert fsm.skeleton_text != "", "骨架应有内容"
        assert "bug.py" in fsm.skeleton_text, "骨架应含 bug.py"
        print("✅ dp：骨架正常注入")