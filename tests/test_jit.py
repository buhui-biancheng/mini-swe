# -*- coding: utf-8 -*-
"""Phase 5 测试：JIT 图谱补全（四层防幻觉防线 + 反射节点真实场景）。"""
import os
import sys

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest


def _make_graph_with_reflection(tmp_path):
    """构造含反射节点的项目图。"""
    from swe_agent.graph.manager import GraphManager

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "service.py").write_text(
        '"""业务模块。"""\n'
        "class Service:\n"
        "    def run(self, method_name):\n"
        "        method = getattr(self, method_name)\n"
        "        return method()\n"
        "    def handle(self):\n"
        "        return \"ok\"\n", encoding="utf-8")
    (proj / "main.py").write_text(
        "from service import Service\n"
        "s = Service()\n"
        "s.run()\n", encoding="utf-8")
    mgr = GraphManager(str(proj))
    idx = mgr.build()
    return mgr, idx


class TestJitValidation:
    def test_accept_valid_reflection(self, tmp_path):
        """合法补全：反射节点 → 目标存在 → accepted。"""
        mgr, idx = _make_graph_with_reflection(tmp_path)
        # 找到反射节点（service.py 里 run 方法应该被标记 is_reflection）
        refl_nodes = [n.node_id for n in idx.graph.nodes.values()
                      if n.is_reflection]
        print("反射节点:", refl_nodes)
        assert refl_nodes, "应有反射节点"
        node_id = refl_nodes[0]
        # 目标：handle 方法
        target = [n.node_id for n in idx.graph.nodes.values()
                  if n.name.endswith("handle")][0]
        result = mgr.apply_jit_update(node_id, target, "call", "读了 service.py 源码，getattr 目标是 handle")
        print("补全结果:", result)
        assert result["accepted"] is True, f"应接受: {result}"
        # 标签更新：不再是反射节点
        assert idx.graph.nodes[node_id].is_reflection is False, "补全后标签应 resolved"
        print("✅ 合法补全 accepted + 标签 resolved")

    def test_reject_nonexistent_target(self, tmp_path):
        mgr, idx = _make_graph_with_reflection(tmp_path)
        refl = [n.node_id for n in idx.graph.nodes.values() if n.is_reflection][0]
        result = mgr.apply_jit_update(refl, "nonexistent.py::foo", "call", "证据")
        assert result["accepted"] is False
        assert "不存在" in result["reason"]
        print("✅ 拒绝不存在的目标")

    def test_reject_non_reflection_source(self, tmp_path):
        mgr, idx = _make_graph_with_reflection(tmp_path)
        # 用 main.py 的节点（非反射）
        src = [n.node_id for n in idx.graph.nodes.values()
               if "main.py" in n.node_id][0]
        tgt = [n.node_id for n in idx.graph.nodes.values()
               if "handle" in n.node_id][0]
        result = mgr.apply_jit_update(src, tgt, "call", "证据")
        assert result["accepted"] is False
        assert "非反射" in result["reason"]
        print("✅ 拒绝非反射源节点")

    def test_reject_self_loop_and_dup(self, tmp_path):
        mgr, idx = _make_graph_with_reflection(tmp_path)
        refl = [n.node_id for n in idx.graph.nodes.values() if n.is_reflection][0]
        # 自环
        r1 = mgr.apply_jit_update(refl, refl, "call", "证据证据")
        assert r1["accepted"] is False
        # 重复提交（第二次）
        tgt = [n.node_id for n in idx.graph.nodes.values()
               if "handle" in n.node_id][0]
        r2 = mgr.apply_jit_update(refl, tgt, "call", "证据证据证据")
        assert r2["accepted"] is True
        r3 = mgr.apply_jit_update(refl, tgt, "call", "证据证据证据")
        assert r3["accepted"] is False
        assert "已存在" in r3["reason"]
        print("✅ 拒绝自环 + 重复边（幂等）")

    def test_reject_weak_evidence(self, tmp_path):
        mgr, idx = _make_graph_with_reflection(tmp_path)
        refl = [n.node_id for n in idx.graph.nodes.values() if n.is_reflection][0]
        tgt = [n.node_id for n in idx.graph.nodes.values()
               if "handle" in n.node_id][0]
        r = mgr.apply_jit_update(refl, tgt, "call", "短")
        assert r["accepted"] is False
        assert "证据" in r["reason"]
        print("✅ 拒绝弱证据")

    def test_registry_tool_roundtrip(self, tmp_path):
        """工具层往返：report_graph_update 可调用。"""
        mgr, idx = _make_graph_with_reflection(tmp_path)
        from swe_agent.tools.registry import ToolRegistry
        reg = ToolRegistry(skeleton_text="", code_dir=str(tmp_path / "proj"),
                           graph_index=idx, graph_manager=mgr)
        refl = [n.node_id for n in idx.graph.nodes.values() if n.is_reflection][0]
        tgt = [n.node_id for n in idx.graph.nodes.values()
               if "handle" in n.node_id][0]
        result = reg.execute("report_graph_update", {
            "node_id": refl, "target": tgt, "edge_type": "call",
            "evidence": "读了 service.py 第 5 行 getattr 调用，目标是 handle"})
        import json
        data = json.loads(result)
        assert data["accepted"] is True, f"工具调用应接受: {data}"
        print("✅ 工具层 roundtrip")