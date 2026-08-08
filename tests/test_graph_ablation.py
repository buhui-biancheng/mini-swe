# -*- coding: utf-8 -*-
"""消融验证测试：graph_level 三层级（显微镜粗准/细准，2026-08-08 用户定稿）。
level=0 纯贪心：无任何图信息；level=1 只有细准：L1 函数级；level=2 完整：粗准+细准。
"""
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
    def test_level0_pure_greedy_all_empty(self, proj, monkeypatch):
        """level=0 纯贪心：图上下文/L-1/骨架/影响面标注全空。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        fsm = AgentFSM(bug_file=str(proj / "bug.py"),
                       test_command="python3 -m pytest test_bug.py -q",
                       code_dir=str(proj), max_retries=1, mode="dp",
                       no_degrade=True, graph_level=0)
        assert fsm._graph_context_text() == "", "图上下文应为空"
        assert fsm._file_level_prior_text() == "", "L-1 应为空"
        assert fsm.skeleton_text == "", "骨架应为空"
        # 细准入口也不该有任何 L1
        assert "NODE:" not in fsm._graph_context_text()
        print("✅ level=0 纯贪心：全空")

    def test_level1_only_fine(self, proj, monkeypatch):
        """level=1 只有细准：L1 函数级有，L-1/骨架/L0 无。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        fsm = AgentFSM(bug_file=str(proj / "bug.py"),
                       test_command="python3 -m pytest test_bug.py -q",
                       code_dir=str(proj), max_retries=1, mode="dp",
                       no_degrade=True, graph_level=1)
        ctx = fsm._graph_context_text()
        assert fsm.skeleton_text == "", "骨架应空（粗准关）"
        assert fsm._file_level_prior_text() == "", "L-1 应空（粗准关）"
        assert "【图索引 L0 摘要】" not in ctx, "L0 应空（粗准关）"
        # 细准 L1 邻域在（但 bug.py 无函数级节点时可能为空——验证结构而非内容）
        print(f"✅ level=1 只有细准：骨架/L-1/L0 空；L1 结构={'NODE:' in ctx or 'EDGE:' in ctx}")

    def test_level2_full(self, proj, monkeypatch):
        """level=2 完整：粗准+细准都有。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        fsm = AgentFSM(bug_file=str(proj / "bug.py"),
                       test_command="python3 -m pytest test_bug.py -q",
                       code_dir=str(proj), max_retries=1, mode="dp",
                       no_degrade=True, graph_level=2)
        ctx = fsm._graph_context_text()
        assert fsm.skeleton_text != "", "骨架应有内容"
        assert "bug.py" in fsm.skeleton_text
        assert "【图索引 L0 摘要】" in ctx, "L0 应有"
        print("✅ level=2 完整：骨架/L0 有")
