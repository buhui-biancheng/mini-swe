# -*- coding: utf-8 -*-
"""多语言图构建测试（2026-08-08 Tree-sitter 扩展）：JS 建图冒烟。"""
import os
import sys

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest


def _build(tmp_path, files):
    from swe_agent.graph.builder import GraphBuilder
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return GraphBuilder(str(tmp_path)).build()


class TestTreeSitterJS:
    def test_js_functions_and_calls(self, tmp_path):
        """JS：函数节点 + 调用边（名字匹配）。"""
        g = _build(tmp_path, {
            "math.js": "function add(a, b) { return a + b; }\n"
                      "function calc() { return add(1, 2); }\n",
        })
        nids = list(g.nodes.keys())
        assert "math.js::add" in nids, f"add 节点缺失: {nids}"
        assert "math.js::calc" in nids
        # 调用边 calc -> add
        edges = g.edges
        call_edges = [e for e in edges if e.edge_type == "call"]
        assert any(e.source == "math.js::calc" and e.target == "math.js::add"
                   for e in call_edges), f"calc->add 调用边缺失: {call_edges}"
        print("✅ JS 函数 + 调用边")

    def test_js_class_methods(self, tmp_path):
        """JS：类 + 方法节点。"""
        g = _build(tmp_path, {
            "app.js": "class App {\n"
                      "  constructor() { this.x = 1; }\n"
                      "  run() { return this.x; }\n"
                      "}\n",
        })
        nids = list(g.nodes.keys())
        assert "app.js::App" in nids
        assert "app.js::App.constructor" in nids
        assert "app.js::App.run" in nids
        print("✅ JS 类 + 方法")

    def test_js_import(self, tmp_path):
        """JS：import 语句解析（symbol 导入）。"""
        from swe_agent.graph.languages.ts_provider import TreeSitterProvider
        p = TreeSitterProvider("javascript")
        raw = p.parse(
            'import { add } from "./math.js";\nadd(1, 2);\n', "main.js")
        assert len(raw.imports) == 1
        imp = raw.imports[0]
        assert imp.kind == "symbol" and imp.symbol == "add" and imp.module == "./math.js"
        print("✅ JS import 解析")

    def test_python_unaffected(self, tmp_path):
        """回归：Python 路径不受多语言接入影响。"""
        g = _build(tmp_path, {
            "mod.py": "def foo(x):\n    return x + 1\n"
                      "def bar():\n    return foo(1)\n",
        })
        nids = list(g.nodes.keys())
        assert "mod.py::foo" in nids and "mod.py::bar" in nids
        edges = g.edges
        assert any(e.edge_type == "call"
                   and e.source == "mod.py::bar" and e.target == "mod.py::foo"
                   for e in edges)
        print("✅ Python 回归")
