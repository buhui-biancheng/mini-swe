"""权限围栏单元测试（Phase 2 模块 B）。"""

import os

import pytest

from swe_agent.graph import AgentConfig, GraphBuilder, GraphIndex, PermissionFence


def _build_fence(tmp_path, files):
    """构造小项目图 + 围栏。files: {文件名: 源码}"""
    for name, code in files.items():
        (tmp_path / name).write_text(code, encoding="utf-8")
    config = AgentConfig()
    builder = GraphBuilder(str(tmp_path), config)
    idx = GraphIndex(builder.build(), config)
    return PermissionFence(idx, config), idx


class TestPermissionFence:
    def test_no_high_risk_in_small_project(self, tmp_path):
        fence, _ = _build_fence(tmp_path, {
            "main.py": "def run():\n    return 1\n",
        })
        assert fence.is_high_risk("main.py") is False
        assert fence.fence_text() == ""

    def test_high_indegree_file_flagged(self, tmp_path):
        # helper 被 main 的多个函数调用 → helper.py 入度总和 > 阈值
        fence, _ = _build_fence(tmp_path, {
            "helper.py": "def util():\n    return 1\n",
            "main.py": (
                "from helper import util\n"
                "def a():\n    return util()\n"
                "def b():\n    return util()\n"
                "def c():\n    return util()\n"
            ),
        })
        # 手动调低阈值验证软约束生效
        fence.config.in_degree_threshold = 2
        fence._build()
        assert fence.is_high_risk("helper.py") is True
        assert "helper.py" in fence.fence_text()

    def test_core_dir_flagged(self, tmp_path):
        core = tmp_path / "core"
        core.mkdir()
        (core / "engine.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        (tmp_path / "main.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        config = AgentConfig()
        builder = GraphBuilder(str(tmp_path), config)
        idx = GraphIndex(builder.build(), config)
        fence = PermissionFence(idx, config)
        assert fence.is_high_risk("core/engine.py") is True

    def test_soft_constraint_allows_edit(self, tmp_path):
        fence, _ = _build_fence(tmp_path, {
            "helper.py": "def util():\n    return 1\n",
            "main.py": (
                "from helper import util\n"
                "def a():\n    return util()\n"
                "def b():\n    return util()\n"
                "def c():\n    return util()\n"
            ),
        })
        fence.config.in_degree_threshold = 2
        fence._build()
        check = fence.check_edit("helper.py")
        # 软约束：警告 + 惩罚，但不拦截
        assert check.allowed is True
        assert check.warnings
        assert check.penalty > 1.0

    def test_penalty_multiplies_impact(self, tmp_path):
        fence, _ = _build_fence(tmp_path, {
            "helper.py": "def util():\n    return 1\n",
            "main.py": (
                "from helper import util\n"
                "def a():\n    return util()\n"
                "def b():\n    return util()\n"
                "def c():\n    return util()\n"
            ),
        })
        fence.config.in_degree_threshold = 2
        fence._build()
        node = fence.graph_index.resolve_location("helper.py", 1)
        assert node is not None
        assert fence.penalty(node.node_id) == pytest.approx(fence.config.fence_penalty)
