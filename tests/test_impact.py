"""变更影响面分析单元测试（Phase 2 模块 C）。"""

import os

import pytest

from swe_agent.graph import (
    AgentConfig,
    GraphBuilder,
    GraphIndex,
    PermissionFence,
    compute_edited_impact,
    resolve_edited_nodes,
)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "helper.py").write_text(
        "def util():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text(
        "from helper import util\n"
        "def run():\n    return util()\n"
        "def work():\n    return util()\n",
        encoding="utf-8",
    )
    config = AgentConfig()
    builder = GraphBuilder(str(tmp_path), config)
    idx = GraphIndex(builder.build(), config)
    return tmp_path, idx, config


class TestResolveEditedNodes:
    def test_hit_function_body(self, project):
        tmp_path, idx, _ = project
        nodes = resolve_edited_nodes(idx, [(str(tmp_path / "helper.py"), 1, 1)])
        assert nodes == ["helper.py::util"]

    def test_miss_function_body(self, project):
        tmp_path, idx, _ = project
        # 行号超出函数体范围 → 不命中
        nodes = resolve_edited_nodes(idx, [(str(tmp_path / "helper.py"), 5, 5)])
        assert nodes == []

    def test_multiple_edits_dedup(self, project):
        tmp_path, idx, _ = project
        nodes = resolve_edited_nodes(idx, [
            (str(tmp_path / "helper.py"), 1, 1),
            (str(tmp_path / "helper.py"), 2, 2),
        ])
        assert nodes == ["helper.py::util"]


class TestComputeEditedImpact:
    def test_impact_positive_for_edited_node(self, project):
        tmp_path, idx, config = project
        result = compute_edited_impact(
            idx, [(str(tmp_path / "helper.py"), 1, 1)], config
        )
        assert result["nodes"] == ["helper.py::util"]
        assert result["total"] >= 0

    def test_fence_penalty_multiplies(self, project):
        tmp_path, idx, config = project
        # 调低阈值让 helper.py 成为高风险文件（入度 2 > 阈值 1）
        config.in_degree_threshold = 1
        fence = PermissionFence(idx, config)
        result = compute_edited_impact(
            idx, [(str(tmp_path / "helper.py"), 1, 1)], config, fence
        )
        assert result["details"][0]["penalty"] == pytest.approx(config.fence_penalty)

    def test_empty_edits_zero_impact(self, project):
        tmp_path, idx, config = project
        result = compute_edited_impact(idx, [], config)
        assert result["total"] == 0
        assert result["nodes"] == []


class TestTestFileExclusion:
    """测试文件识别 + 影响面排除测试节点（Phase 2 收尾补丁）。"""

    @pytest.fixture
    def proj_with_tests(self, tmp_path):
        (tmp_path / "prod.py").write_text(
            "def compute():\n    return 1\n"
            "def main():\n    return compute()\n",
            encoding="utf-8",
        )
        (tmp_path / "test_prod.py").write_text(
            "from prod import compute\n"
            "def test_compute():\n    assert compute() == 1\n",
            encoding="utf-8",
        )
        config = AgentConfig()
        builder = GraphBuilder(str(tmp_path), config)
        return GraphIndex(builder.build(), config)

    def test_is_test_file_rules(self, proj_with_tests):
        idx = proj_with_tests
        assert idx.is_test_file("test_prod.py") is True      # test_ 前缀
        assert idx.is_test_file("prod_tests.py") is True     # _tests 后缀
        assert idx.is_test_file("tests/foo.py") is True      # tests/ 目录
        assert idx.is_test_file("prod.py") is False
        assert idx.is_test_file("testing.py") is False       # 不误伤 testing.py

    def test_impact_excludes_test_callers(self, proj_with_tests):
        """测试调用生产函数 → 测试节点不计入生产影响面。"""
        idx = proj_with_tests
        detail = idx.compute_impact_detail("prod.py::compute")
        nodes = {d["node"] for d in detail["details"]}
        assert "prod.py::main" in nodes          # 生产调用方计入
        assert not any("test_prod" in n for n in nodes)  # 测试调用方排除

    def test_summary_excludes_test_nodes(self, proj_with_tests):
        idx = proj_with_tests
        summary = idx.get_summary()
        assert not any("test_prod" in item["node"] for item in summary["top_in_degree"])

    def test_get_callers_still_shows_tests(self, proj_with_tests):
        """get_callers 保留测试调用方（AI 需要知道"哪个测试覆盖我"）。"""
        idx = proj_with_tests
        callers = idx.get_callers("prod.py::compute")
        names = {c.node_id for c in callers}
        assert "prod.py::main" in names
        assert "test_prod.py::test_compute" in names
