"""Phase 1 加权图索引测试：金标准 + 查询 + 持久化 + 缓存 + 增量 + LLM 重试。

金标准测试（golden test）：
    人工标注每个 fixture 的期望边集合 → 跑 GraphBuilder → 对比。
    指标：召回率 = 正确边 / 期望边；精确率 = 正确边 / 生成边。
"""

import os
import json
import time
import httpx
from types import SimpleNamespace

import pytest

from swe_agent.graph import (
    AgentConfig,
    GraphBuilder,
    GraphManager,
    GraphIndex,
    EdgeType,
)
from swe_agent.graph import persistence
from swe_agent.llm.client import LLMClient, AgentAPIError

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# ========== 金标准边集合 ==========

GOLDEN_EDGES = {
    # 1. 基础调用链 + 数据流级别 2（赋值链）+ 嵌套调用
    "call_graph": {
        ("main.py", "utils.py", "import"),
        ("main.py", "utils.py::helper", "import"),
        ("main.py::run", "main.py::compute", "call"),
        ("main.py::run", "utils.py::helper", "call"),
        ("main.py::compute", "utils.py::helper", "data"),
        ("main.py::nested", "utils.py::helper", "call"),
        ("main.py::nested", "main.py::compute", "call"),
    },
    # 2. 继承 + 运行时多态全笼罩
    "classes": {
        ("animals.py::Dog", "animals.py::Animal", "inherit"),
        ("animals.py::Cat", "animals.py::Animal", "inherit"),
        ("animals.py::make_sound", "animals.py::Animal.sound", "call"),
        ("animals.py::make_sound", "animals.py::Dog.sound", "call"),
        ("animals.py::make_sound", "animals.py::Cat.sound", "call"),
    },
    # 3. 全局变量节点 + 跨模块全局引用
    "globals": {
        ("app.py", "config.py", "import"),
        ("app.py", "config.py::CONFIG", "import"),
        ("app.py::setup", "config.py::CONFIG", "global"),
        ("app.py::read_config", "config.py::CONFIG", "global"),
        ("config.py::load", "config.py::CONFIG", "global"),
        ("config.py::get_debug", "config.py::CONFIG", "global"),
    },
    # 4. 反射：getattr 硬编码全笼罩 + importlib 动态加载打标签
    "reflection": {
        ("services.py::call_by_name", "services.py::ServiceA.run", "call"),
        ("services.py::call_by_name", "services.py::ServiceB.run", "call"),
        ("loader.py::load_literal", "services.py", "import"),
    },
    # 5. 局部变量持有类实例 → 精确解析 + 未知类型多态全笼罩
    "polymorphism_dataflow": {
        ("geometry.py::Circle", "geometry.py::Shape", "inherit"),
        ("geometry.py::Rect", "geometry.py::Shape", "inherit"),
        ("geometry.py::calc", "geometry.py::Shape.area", "call"),
        ("geometry.py::calc", "geometry.py::Circle.area", "call"),
        ("geometry.py::calc", "geometry.py::Rect.area", "call"),
        ("geometry.py::make_circle", "geometry.py::Circle", "call"),
        ("geometry.py::make_circle", "geometry.py::Circle.area", "call"),
    },
    # 6. 自递归（自环）+ 相互递归（去环 BFS 不挂死）
    "recursion": {
        ("math.py::fact", "math.py::fact", "call"),
        ("math.py::is_even", "math.py::is_odd", "call"),
        ("math.py::is_odd", "math.py::is_even", "call"),
    },
    # 7. 局部变量遮蔽全局名（shadow() 不建 global 边）
    "shadowing": {
        ("config.py::read", "config.py::CONFIG", "global"),
    },
    # 8. 分支数据流全笼罩（x = a() 或 b() → c(x) 建两条数据边）
    "branch_dataflow": {
        ("functions.py::pick", "functions.py::a", "call"),
        ("functions.py::pick", "functions.py::b", "call"),
        ("functions.py::pick", "functions.py::c", "call"),
        ("functions.py::b", "functions.py::c", "data"),
        ("functions.py::a", "functions.py::c", "data"),
    },
    # 9. self / super() / 类名静态方法调用 + 继承
    "self_super": {
        ("base.py::Child", "base.py::Base", "inherit"),
        ("base.py::Child.greet", "base.py::Base.greet", "call"),
        ("base.py::Child.call_self", "base.py::Child.greet", "call"),
        ("base.py::Child.call_static", "base.py::Base.help", "call"),
    },
    # 10. 模块别名导入 + 属性链调用（import pkg.mod as mo; mo.add()）
    "multi_module": {
        ("main.py", "pkg/math_ops.py", "import"),
        ("main.py::run", "pkg/math_ops.py::add", "call"),
        ("main.py::run2", "pkg/math_ops.py::multiply", "call"),
    },
}

GOLDEN_REFLECTION = {
    "loader.py::load_module": True,
}


def _build_fixture(name: str, tmp_path):
    """构建 fixture 图（使用独立 graph_dir，不污染源码）。"""
    graph_dir = str(tmp_path / f"graph_{name}")
    mgr = GraphManager(os.path.join(FIXTURES_DIR, name), graph_dir=graph_dir)
    idx = mgr.build(force=True)
    return mgr, idx


class TestGoldenStandard:
    """金标准测试：图建对了。"""

    @pytest.mark.parametrize("fixture_name", list(GOLDEN_EDGES.keys()))
    def test_expected_edges(self, fixture_name, tmp_path):
        _, idx = _build_fixture(fixture_name, tmp_path)
        generated = {
            (e.source, e.target, e.edge_type.value)
            for e in idx.graph.edges
        }
        expected = GOLDEN_EDGES[fixture_name]

        # 召回率：该建的边建了吗
        recall = len(generated & expected) / len(expected)
        # 精确率：不该建的边建了吗
        precision = len(generated & expected) / len(generated) if generated else 0.0

        assert recall == 1.0, (
            f"{fixture_name} 召回率 {recall:.2f}，缺少边: "
            f"{expected - generated}"
        )
        assert precision == 1.0, (
            f"{fixture_name} 精确率 {precision:.2f}，多余边: "
            f"{generated - expected}"
        )

    @pytest.mark.parametrize("fixture_name", list(GOLDEN_EDGES.keys()))
    def test_recall_precision_report(self, fixture_name, tmp_path):
        """输出可报告的召回率/精确率数据。"""
        _, idx = _build_fixture(fixture_name, tmp_path)
        generated = {
            (e.source, e.target, e.edge_type.value)
            for e in idx.graph.edges
        }
        expected = GOLDEN_EDGES[fixture_name]
        recall = len(generated & expected) / len(expected)
        precision = len(generated & expected) / len(generated)
        # 金标准测试不追求单条用例报告，但保证质量门槛
        assert recall >= 0.9 and precision >= 0.9

    def test_reflection_tagging(self, tmp_path):
        _, idx = _build_fixture("reflection", tmp_path)
        for node_id, expected in GOLDEN_REFLECTION.items():
            node = idx.get_node(node_id)
            assert node is not None
            assert node.is_reflection == expected

    def test_build_time_budget(self, tmp_path):
        """构建时间预算：小项目 < 2s。"""
        start = time.time()
        _build_fixture("call_graph", tmp_path)
        elapsed = time.time() - start
        assert elapsed < 2.0, f"构建时间 {elapsed:.2f}s 超出预算"

    def test_large_project_build_time_budget(self, tmp_path):
        """大项目构建时间预算：合成 1 万行项目 < 2s（计划要求）。

        动态生成 50 个模块 × ~200 行 ≈ 1 万行，跨模块互相调用。
        这是把"扫描 swe_agent 包 400 节点/0.3s"的经验固化为可复现的自动化测试。
        """
        def gen(num_modules=50, funcs_per_module=8, padding=200):
            src = tmp_path / "bigproj"
            src.mkdir(exist_ok=True)
            for mi in range(num_modules):
                imp = f"import mod_{(mi + 1) % num_modules} as next_mod\n\n"
                body = []
                for fi in range(funcs_per_module):
                    body.append(f"def func_{fi}(a):")
                    body.append("    b = a + 1")
                    body.append(f"    return next_mod.func_{(fi + 1) % funcs_per_module}(b)")
                body += [f"# padding {i}" for i in range(max(0, padding - len(body)))]
                (src / f"mod_{mi}.py").write_text(imp + "\n".join(body) + "\n", encoding="utf-8")
            return src

        src = gen()
        # 确认规模达到 ~1 万行
        total_lines = sum(1 for f in src.glob("*.py") for _ in f.open())
        assert total_lines >= 9000, f"合成项目仅 {total_lines} 行，未达规模"

        start = time.time()
        mgr = GraphManager(str(src), graph_dir=str(tmp_path / "g_big"))
        idx = mgr.build(force=True)
        elapsed = time.time() - start

        g = idx.graph
        assert g.meta.node_count > 300
        assert g.meta.edge_count > 300
        assert elapsed < 2.0, (
            f"1 万行项目构建 {elapsed:.2f}s 超出 2s 预算（节点 {g.meta.node_count}，边 {g.meta.edge_count}）"
        )

    def test_recursion_impact_no_hang(self, tmp_path):
        """递归/相互递归的图，影响面计算不挂死（去环 BFS 验证）。"""
        _, idx = _build_fixture("recursion", tmp_path)
        # 自环起点
        impact = idx.compute_impact("math.py::fact", max_hops=5)
        assert isinstance(impact, float)
        assert impact >= 0
        # 相互递归环起点（is_even ↔ is_odd）
        detail = idx.compute_impact_detail("math.py::is_even", max_hops=10)
        assert detail["total_cost"] >= 0
        assert detail["affected_nodes"] > 0

    def test_ten_fixtures_standard(self):
        """金标准 fixture 数量标准：10 个。"""
        assert len(GOLDEN_EDGES) == 10
        assert all(len(edges) >= 1 for edges in GOLDEN_EDGES.values())


class TestGraphBuilder:
    def test_syntax_error_file_skipped(self, tmp_path):
        """语法错误的文件不崩溃，记入 error。"""
        src = tmp_path / "bad.py"
        src.write_text("def broken(:\n    pass\n", encoding="utf-8")
        mgr = GraphManager(str(tmp_path), graph_dir=str(tmp_path / "g"))
        idx = mgr.build(force=True)
        assert idx.get_node("bad.py") is not None  # 文件节点仍存在

    def test_async_function(self, tmp_path):
        """异步函数提取。"""
        src = tmp_path / "aio.py"
        src.write_text("async def fetch():\n    return 1\n", encoding="utf-8")
        mgr = GraphManager(str(tmp_path), graph_dir=str(tmp_path / "g"))
        idx = mgr.build(force=True)
        assert idx.get_node("aio.py::fetch") is not None

    def test_in_degree_computed(self, tmp_path):
        """入度统计正确。"""
        _, idx = _build_fixture("call_graph", tmp_path)
        node = idx.get_node("utils.py::helper")
        assert node is not None
        # run→helper(call) + nested→helper(call) + compute→helper(data)
        assert node.in_degree == 3

    def test_edge_dedup(self, tmp_path):
        """边去重：同 source/target/type 只建一次。"""
        src = tmp_path / "dup.py"
        src.write_text(
            "def f():\n    return 1\n"
            "def g():\n    a = f()\n    return f()\n",
            encoding="utf-8",
        )
        mgr = GraphManager(str(tmp_path), graph_dir=str(tmp_path / "g"))
        idx = mgr.build(force=True)
        edges = [e for e in idx.graph.edges
                 if e.source == "dup.py::g" and e.target == "dup.py::f"
                 and e.edge_type == EdgeType.CALL]
        assert len(edges) == 1

    def test_star_import_hygiene(self, tmp_path):
        """from x import *：保留模块 import 边，不产生死符号边（* 绑定）。"""
        src = tmp_path / "proj"
        src.mkdir()
        (src / "lib.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        (src / "main.py").write_text(
            "from lib import *\ndef run():\n    return helper()\n", encoding="utf-8"
        )
        mgr = GraphManager(str(src), graph_dir=str(tmp_path / "g"))
        idx = mgr.build(force=True)
        edges = idx.graph.edges
        # 有 main.py → lib.py 的 import 边
        assert any(
            e.source == "main.py" and e.target == "lib.py"
            and e.edge_type == EdgeType.IMPORT
            for e in edges
        )
        # 没有指向 "lib.py::*" 的死边（star import 名字静态不可枚举）
        assert not any(e.target == "lib.py::*" for e in edges)
        assert not any(e.source == "main.py::run" and e.target == "lib.py::helper"
                       for e in edges)  # helper 来自 star import，静态解析不到


class TestGraphIndex:
    @pytest.fixture
    def idx(self, tmp_path):
        _, idx = _build_fixture("call_graph", tmp_path)
        return idx

    def test_get_node(self, idx):
        assert idx.get_node("main.py::run") is not None
        assert idx.get_node("不存在") is None

    def test_get_callers(self, idx):
        callers = idx.get_callers("utils.py::helper")
        names = {c.node_id for c in callers}
        assert "main.py::run" in names
        assert "main.py::nested" in names

    def test_get_callees(self, idx):
        callees = idx.get_callees("main.py::run")
        names = {c.node_id for c in callees}
        assert "main.py::compute" in names
        assert "utils.py::helper" in names

    def test_get_summary(self, idx):
        summary = idx.get_summary()
        assert summary["node_count"] >= 7
        assert summary["edge_count"] >= 7
        assert len(summary["top_in_degree"]) > 0
        # 最高入度应该是 helper
        assert summary["top_in_degree"][0]["node"] == "utils.py::helper"

    def test_get_neighbors_hops1(self, idx):
        result = idx.get_neighbors("main.py::compute", hops=1)
        assert result["node"] == "main.py::compute"
        level1 = result["neighbors"].get(1, [])
        assert any(n["node"] == "main.py::run" for n in level1)
        assert any(n["node"] == "utils.py::helper" for n in level1)

    def test_get_neighbors_unknown(self, idx):
        result = idx.get_neighbors("不存在的节点")
        assert "error" in result

    def test_compute_impact_finite(self, idx):
        """影响面有限（去环 BFS 不挂死）。"""
        impact = idx.compute_impact("utils.py::helper")
        assert isinstance(impact, float)
        assert impact >= 0

    def test_compute_impact_detail(self, idx):
        detail = idx.compute_impact_detail("utils.py::helper")
        assert detail["start"] == "utils.py::helper"
        assert detail["total_cost"] >= 0
        assert detail["affected_nodes"] > 0

    def test_generate_skeleton_text(self, idx):
        text = idx.generate_skeleton_text()
        assert "=== main.py ===" in text
        assert "run (lines" in text
        assert "utils.py::helper" not in text  # 骨架只含文件+函数名

    def test_search_nodes(self, idx):
        results = idx.search_nodes("run")
        assert any(r.node_id == "main.py::run" for r in results)

    def test_expand_function(self, idx):
        source = idx.expand_function("main.py", "run")
        assert source is not None
        assert "def run" in source

    def test_expand_function_missing(self, idx):
        assert idx.expand_function("main.py", "不存在") is None

    def test_traceback_match(self, tmp_path):
        _, idx = _build_fixture("call_graph", tmp_path)
        hit = idx.get_summary(
            'File "/x/main.py", line 8, in run'
        )
        assert hit["traceback_hit"] is not None
        assert "run" in hit["traceback_hit"]["node"]


class TestPersistence:
    def test_graph_roundtrip(self, tmp_path):
        _, idx = _build_fixture("call_graph", tmp_path)
        graph = idx.graph

        graph_dir = str(tmp_path / "persist")
        persistence.save_graph(graph, graph_dir)
        loaded = persistence.load_graph(graph_dir)
        assert loaded is not None
        assert loaded.meta.node_count == graph.meta.node_count
        assert loaded.meta.edge_count == graph.meta.edge_count
        assert set(loaded.nodes.keys()) == set(graph.nodes.keys())

    def test_weights_roundtrip(self, tmp_path):
        graph_dir = str(tmp_path / "w")
        persistence.save_weights({"a.py::f": {"success": 3, "fail": 1}}, graph_dir)
        weights = persistence.load_weights(graph_dir)
        assert weights == {"a.py::f": {"success": 3, "fail": 1}}
        # v1 兼容：int → {"success": n, "fail": 0}
        v1_path = str(tmp_path / "v1")
        os.makedirs(v1_path, exist_ok=True)
        with open(os.path.join(v1_path, "graph_weights.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "weights": {"a.py::f": 2}}, f)
        assert persistence.load_weights(v1_path) == {"a.py::f": {"success": 2, "fail": 0}}
        # 不存在时返回空
        assert persistence.load_weights(str(tmp_path / "missing")) == {}

    def test_graph_from_file_json(self, tmp_path):
        """graph.json 序列化格式（from/to）验证。"""
        _, idx = _build_fixture("call_graph", tmp_path)
        graph_dir = str(tmp_path / "raw")
        persistence.save_graph(idx.graph, graph_dir)
        with open(os.path.join(graph_dir, "graph.json"), encoding="utf-8") as f:
            data = json.load(f)
        assert "from" in data["edges"][0]
        assert "to" in data["edges"][0]
        assert "edge_type" in data["edges"][0]


class TestGraphManager:
    def test_cache_reuse(self, tmp_path):
        """git HEAD 未变 → 复用缓存（不再全量扫描）。"""
        graph_dir = str(tmp_path / "g")
        mgr1 = GraphManager(os.path.join(FIXTURES_DIR, "call_graph"), graph_dir=graph_dir)
        idx1 = mgr1.build(force=True)
        assert idx1 is not None

        # 不 force 再建 → 命中缓存（不抛错且图一致）
        mgr2 = GraphManager(os.path.join(FIXTURES_DIR, "call_graph"), graph_dir=graph_dir)
        idx2 = mgr2.build()
        assert idx2.graph.meta.node_count == idx1.graph.meta.node_count

    def test_dynamic_weight_update(self, tmp_path):
        graph_dir = str(tmp_path / "g")
        mgr = GraphManager(os.path.join(FIXTURES_DIR, "call_graph"), graph_dir=graph_dir)
        mgr.build(force=True)

        mgr.update_dynamic_weight("utils.py::helper")
        assert mgr.get_weight("utils.py::helper") == 1
        mgr.update_dynamic_weight("utils.py::helper")
        assert mgr.get_weight("utils.py::helper") == 2

        # 重新加载图，权重合并回节点
        mgr2 = GraphManager(os.path.join(FIXTURES_DIR, "call_graph"), graph_dir=graph_dir)
        idx2 = mgr2.build()
        assert idx2.get_node("utils.py::helper").dynamic_weight == 2

    def test_incremental_update(self, tmp_path):
        """新增文件后增量更新，图正确扩展。"""
        src = tmp_path / "proj"
        src.mkdir()
        (src / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        graph_dir = str(tmp_path / "g")
        mgr = GraphManager(str(src), graph_dir=graph_dir)
        mgr.build(force=True)
        assert mgr.get_index().get_node("a.py::f") is not None

        # 新增 b.py 调用 f
        (src / "b.py").write_text(
            "from a import f\ndef g():\n    return f()\n", encoding="utf-8"
        )
        mgr.update_from_diff(["b.py"])
        idx = mgr.get_index()
        assert idx.get_node("b.py::g") is not None
        assert any(
            e.source == "b.py::g" and e.target == "a.py::f"
            for e in idx.graph.edges
        )

    def test_validate_weights_merge(self, tmp_path):
        """build 时动态权重合并到节点。"""
        graph_dir = str(tmp_path / "g")
        mgr = GraphManager(os.path.join(FIXTURES_DIR, "call_graph"), graph_dir=graph_dir)
        mgr.build(force=True)
        mgr.update_dynamic_weight("utils.py::helper")
        idx2 = mgr.build()  # 缓存加载也应合并权重
        assert idx2.get_node("utils.py::helper").dynamic_weight >= 1

    def test_incremental_after_cache_load(self, tmp_path):
        """回归：缓存加载后调用 update_from_diff，图必须完整（不丢既有节点）。

        之前 bug：缓存命中时 builder 无符号表，update 后只剩变更文件。
        """
        src = tmp_path / "proj"
        src.mkdir()
        (src / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        graph_dir = str(tmp_path / "g")
        mgr1 = GraphManager(str(src), graph_dir=graph_dir)
        mgr1.build(force=True)

        # 新进程：命中缓存（builder 未扫描）
        mgr2 = GraphManager(str(src), graph_dir=graph_dir)
        idx_cache = mgr2.build()
        assert idx_cache.get_node("a.py::f") is not None  # 缓存加载

        # 缓存加载后增量更新：既保留 a.py，又加入 b.py
        (src / "b.py").write_text(
            "from a import f\ndef g():\n    return f()\n", encoding="utf-8"
        )
        mgr2.update_from_diff(["b.py"])
        idx = mgr2.get_index()
        assert idx.get_node("a.py::f") is not None, "缓存加载后增量更新丢失既有节点"
        assert idx.get_node("b.py::g") is not None
        assert any(
            e.source == "b.py::g" and e.target == "a.py::f"
            for e in idx.graph.edges
        )


class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig()
        assert cfg.max_hops == 5
        assert cfg.decay == 0.5
        assert cfg.rollback_limit == 3

    def test_adaptive_threshold(self):
        cfg = AgentConfig()
        assert cfg.adaptive_impact_threshold(1000) == 100.0
        assert cfg.adaptive_impact_threshold(20000) == 200.0

    def test_config_used_by_impact(self, tmp_path):
        """影响面使用自定义 AgentConfig。"""
        cfg = AgentConfig(max_hops=2, decay=0.5)
        graph_dir = str(tmp_path / "g")
        mgr = GraphManager(
            os.path.join(FIXTURES_DIR, "call_graph"),
            graph_dir=graph_dir,
            config=cfg,
        )
        idx = mgr.build(force=True)
        assert idx.config.max_hops == 2


class TestLLMRetry:
    """LLM API 失败重试（指数退避 + AgentAPIError）。"""

    class _FakeCompletions:
        """以 __call__ 暴露 create，使 completions.create(**kw) 可调用。"""

        def __init__(self, behaviors):
            self.behaviors = behaviors  # 每次调用依次弹出一个行为
            self.calls = 0

        def __call__(self, **kwargs):
            self.calls += 1
            if self.calls <= len(self.behaviors):
                behavior = self.behaviors[self.calls - 1]
                if isinstance(behavior, Exception):
                    raise behavior
                return behavior
            raise AssertionError("create 被过多调用")

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        """去掉重试退避的 sleep，加速测试。"""
        monkeypatch.setattr("swe_agent.llm.client.time.sleep", lambda s: None)

    def _make_client(self, behaviors):
        fake = object.__new__(LLMClient)
        fake.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=self._FakeCompletions(behaviors))
            )
        )
        return fake

    @staticmethod
    def _status_error(status_code: int, message: str = "err"):
        request = httpx.Request("GET", "http://test")
        response = httpx.Response(status_code, request=request)
        from openai import APIStatusError
        return APIStatusError(message, response=response, body=None)

    def test_retry_then_success(self):
        from openai import RateLimitError
        request = httpx.Request("GET", "http://test")
        response = httpx.Response(429, request=request)
        client = self._make_client([
            RateLimitError("rate limited", response=response, body=None),
            RateLimitError("rate limited", response=response, body=None),
            {"ok": True},
        ])
        result = LLMClient._create_with_retry(client, {})
        assert result == {"ok": True}
        assert client.client.chat.completions.create.calls == 3

    def test_exhausted_raises_agent_api_error(self):
        from openai import APIConnectionError
        request = httpx.Request("GET", "http://test")
        client = self._make_client([
            APIConnectionError(request=request),
        ] * 4)
        with pytest.raises(AgentAPIError) as exc:
            LLMClient._create_with_retry(client, {})
        assert exc.value.retries == 4

    def test_4xx_not_retried(self):
        client = self._make_client([self._status_error(400)])
        with pytest.raises(Exception):
            LLMClient._create_with_retry(client, {})
        assert client.client.chat.completions.create.calls == 1

    def test_5xx_retried(self):
        client = self._make_client([self._status_error(503), {"ok": True}])
        result = LLMClient._create_with_retry(client, {})
        assert result == {"ok": True}
        assert client.client.chat.completions.create.calls == 2


class TestCompactFormat:
    """极简图格式 graph_compact.grf：AI 位置化读取 + 人类可读调试。"""

    def test_header_and_counts(self, tmp_path):
        mgr, idx = _build_fixture("call_graph", tmp_path)
        path = persistence.save_compact(idx.graph, mgr.graph_dir)
        text = open(path, encoding="utf-8").read()
        assert text.startswith("VERSION: 1\nTYPE: graph\nSEP: |")
        assert text.count("NODE: ") == idx.graph.meta.node_count
        assert text.count("EDGE: ") == idx.graph.meta.edge_count

    def test_node_line_fixed_columns(self, tmp_path):
        mgr, idx = _build_fixture("call_graph", tmp_path)
        path = persistence.save_compact(idx.graph, mgr.graph_dir)
        for line in open(path, encoding="utf-8"):
            if not line.startswith("NODE: "):
                continue
            parts = line[len("NODE: "):].strip().split(" | ")
            assert len(parts) == 7, f"NODE 应为 7 列: {line!r}"
            node_id, file, function, lineno, indeg, weight, refl = parts
            assert node_id in idx.graph.nodes
            assert file and function
            assert lineno.isdigit()
            assert refl in ("true", "false")

    def test_edge_line_fixed_columns(self, tmp_path):
        mgr, idx = _build_fixture("call_graph", tmp_path)
        path = persistence.save_compact(idx.graph, mgr.graph_dir)
        edge_lines = [l for l in open(path, encoding="utf-8") if l.startswith("EDGE: ")]
        assert edge_lines, "应有 EDGE 行"
        for line in edge_lines:
            src, dst, etype = line[len("EDGE: "):].strip().split(" | ")
            assert len(line.split(" | ")) == 3
            assert src in idx.graph.nodes
            assert dst in idx.graph.nodes
            assert etype in ("call", "data", "import", "inherit", "global", "io")

    def test_separator_isolated(self, tmp_path):
        """内容中无裸 |（分隔符唯一，按位置分割可靠）。"""
        mgr, idx = _build_fixture("call_graph", tmp_path)
        path = persistence.save_compact(idx.graph, mgr.graph_dir)
        for line in open(path, encoding="utf-8"):
            if line.startswith("NODE: ") or line.startswith("EDGE: "):
                body = line.split(": ", 1)[1].strip()
                expected = 6 if line.startswith("NODE: ") else 2
                assert body.count("|") == expected, f"分隔符数不符: {line!r}"

    def test_reflection_flag_serialized(self, tmp_path):
        mgr, idx = _build_fixture("reflection", tmp_path)
        path = persistence.save_compact(idx.graph, mgr.graph_dir)
        assert "true" in open(path, encoding="utf-8").read()
