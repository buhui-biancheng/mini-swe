"""CLI 辅助函数：graph build / stats / viz（Phase 1）。

面试演示依赖这些命令输出结构化统计，支撑 A/B 评测与问题复盘。
"""

import os
import time

from swe_agent.graph import AgentConfig, GraphManager
from swe_agent.graph.persistence import save_compact


def graph_build(project_dir: str, force: bool = False) -> None:
    """构建/重建图索引，输出统计。"""
    project_dir = os.path.abspath(project_dir)
    print(f"[GRAPH] 构建图索引: {project_dir} (force={force})")

    start = time.time()
    mgr = GraphManager(project_dir)
    idx = mgr.build(force=force)
    elapsed = (time.time() - start) * 1000

    g = idx.graph
    print(f"[GRAPH] 完成（{elapsed:.0f}ms）")
    print(f"  节点数: {g.meta.node_count}")
    print(f"  边数:   {g.meta.edge_count}")
    print(f"  最大入度: {g.meta.max_in_degree}")
    print(f"  git HEAD: {g.meta.git_commit or '(非 git 仓库)'}")
    print(f"  缓存目录: {mgr.graph_dir}")

    summary = idx.get_summary()
    print("\n[GRAPH] Top 高入度节点:")
    for item in summary["top_in_degree"][:10]:
        print(f"  - {item['node']} (in_degree={item['in_degree']})")


def graph_stats(project_dir: str) -> None:
    """图统计：节点数、边数、Top10 高入度节点。"""
    project_dir = os.path.abspath(project_dir)
    mgr = GraphManager(project_dir)
    idx = mgr.build()
    g = idx.graph
    summary = idx.get_summary()

    print(f"[GRAPH] 统计: {project_dir}")
    print(f"  节点数:   {g.meta.node_count}")
    print(f"  边数:     {g.meta.edge_count}")
    print(f"  文件数:   {summary['file_count']}")
    print(f"  函数数:   {summary['function_count']}")
    print(f"  最大入度: {g.meta.max_in_degree}")
    print(f"  git HEAD: {g.meta.git_commit or '(非 git 仓库)'}")

    print("\n[GRAPH] Top 高入度节点（核心枢纽）:")
    for i, item in enumerate(summary["top_in_degree"][:10], 1):
        print(f"  {i}. {item['node']} (in_degree={item['in_degree']}, "
              f"dynamic_weight={item['dynamic_weight']})")

    # 动态权重汇总
    weights = mgr.load_weights()
    if weights:
        print("\n[GRAPH] 动态权重（历史修复活跃度）:")
        for nid, w in sorted(weights.items(), key=lambda x: -x[1])[:10]:
            print(f"  - {nid}: +{w}")


def graph_viz(project_dir: str, format: str = "mermaid", output: str = "") -> None:
    """导出图可视化（mermaid/dot 格式）。"""
    project_dir = os.path.abspath(project_dir)
    mgr = GraphManager(project_dir)
    idx = mgr.build()
    g = idx.graph

    if format == "mermaid":
        text = _to_mermaid(g)
    else:
        text = _to_dot(g)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[GRAPH] 已导出到 {output}")
    else:
        print(text)


def graph_compact(project_dir: str) -> None:
    """导出极简图格式 graph_compact.grf（AI 位置化读取 + 人类可读调试）。

    格式规范：头部 VERSION/TYPE/SEP 一次性定义；行首 NODE:/EDGE: 区分节点边；
    字段按固定列序排列，AI 按位置提取无需理解字段名。
    """
    project_dir = os.path.abspath(project_dir)
    mgr = GraphManager(project_dir)
    idx = mgr.build()
    g = idx.graph
    path = save_compact(g, mgr.graph_dir)
    size = os.path.getsize(path)
    print(f"[GRAPH] 已导出极简格式: {path}")
    print(f"  节点 {g.meta.node_count} / 边 {g.meta.edge_count} / {size} 字节")


def _safe_id(node_id: str) -> str:
    """mermaid/dot 安全的节点标识。"""
    return node_id.replace("::", "__").replace(".", "_").replace("/", "_")


def _to_mermaid(g) -> str:
    lines = ["flowchart LR"]
    for nid in g.nodes:
        lines.append(f'    {_safe_id(nid)}["{nid}"]')
    lines.append("")
    for e in g.edges:
        color = {
            "call": "#2563eb",
            "data": "#16a34a",
            "import": "#d97706",
            "inherit": "#7c3aed",
            "global": "#dc2626",
            "io": "#64748b",
        }.get(e.edge_type.value, "#000000")
        lines.append(
            f'    {_safe_id(e.source)} -->|"{e.edge_type.value}"| {_safe_id(e.target)}'
            f'\n    style {_safe_id(e.source)} stroke:{color}'
        )
    return "\n".join(lines)


def _to_dot(g) -> str:
    lines = ["digraph swe_agent {"]
    for nid, node in g.nodes.items():
        lines.append(
            f'    "{_safe_id(nid)}" [label="{nid}", shape='
            f'{"box" if node.node_type.value == "function" else "ellipse"}];'
        )
    lines.append("")
    for e in g.edges:
        lines.append(f'    "{_safe_id(e.source)}" -> "{_safe_id(e.target)}" '
                     f'[label="{e.edge_type.value}"];')
    lines.append("}")
    return "\n".join(lines)
