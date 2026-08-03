"""变更影响面分析（Phase 2 模块 C / CHECK 前置动作）。

把 edit_function 的实际编辑行范围解析到图节点 → 累加影响面。
ROLLBACK 代价熔断的判定依据，也是 PATCH→TEST 之间检查调用方的数据来源。

确定性保证：编辑行范围 ∩ 函数体范围 = 语法确定的交集，无歧义。
"""

import os

from .config import AgentConfig


def resolve_edited_nodes(graph_index, edited_ranges: list[tuple]) -> list[str]:
    """编辑行范围 → 图函数节点 id 列表（去重，保持顺序）。

    匹配规则：编辑区间 [start, end] 与函数体 [lineno, end_lineno] 有交集。
    """
    targets = []
    for file_path, start, end in edited_ranges:
        base = os.path.basename(file_path)
        for n in graph_index.graph.nodes.values():
            if n.node_type.value != "function":
                continue
            if os.path.basename(n.file) != base:
                continue
            if n.lineno <= end and start <= n.end_lineno:
                targets.append(n.node_id)
    return list(dict.fromkeys(targets))


def compute_edited_impact(graph_index, edited_ranges: list[tuple],
                          config=None, fence=None) -> dict:
    """累加本次编辑涉及的节点影响面（含围栏惩罚）。

    Returns:
        {"total": float, "details": [{node, impact, penalty, cost}], "nodes": [id]}
    """
    config = config or AgentConfig()
    nodes = resolve_edited_nodes(graph_index, edited_ranges)
    total = 0.0
    details = []
    for nid in nodes:
        detail = graph_index.compute_impact_detail(nid)
        penalty = fence.penalty(nid) if fence else 1.0
        cost = detail["total_cost"] * penalty
        total += cost
        details.append({
            "node": nid,
            "impact": detail["total_cost"],
            "penalty": penalty,
            "cost": round(cost, 6),
        })
    return {
        "total": round(total, 6),
        "details": details,
        "nodes": nodes,
    }
