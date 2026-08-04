"""图持久化：graph.json / graph_weights.json / graph_compact.grf 读写。

graph.json       — 图结构（节点 + 边 + 入度），随 git HEAD 变化重建
graph_weights.json — 动态权重（独立持久化），累积修改历史
graph_compact.grf — 极简图格式导出（AI 位置化读取 + 人类可读调试），体积小
"""

import json
import os
from typing import Optional

from .models import GraphData

WEIGHTS_FILENAME = "graph_weights.json"
GRAPH_FILENAME = "graph.json"
COMPACT_FILENAME = "graph_compact.grf"


def _resolve_path(graph_dir: str, filename: str) -> str:
    return os.path.join(graph_dir, filename)


def save_graph(graph: GraphData, graph_dir: str) -> str:
    """保存 graph.json（by_alias 序列化为 from/to 格式）。"""
    os.makedirs(graph_dir, exist_ok=True)
    path = _resolve_path(graph_dir, GRAPH_FILENAME)
    graph.refresh_stats()
    data = {
        "meta": graph.meta.model_dump(),
        "nodes": {k: v.model_dump() for k, v in graph.nodes.items()},
        "edges": [e.to_dict() for e in graph.edges],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_graph(graph_dir: str) -> Optional[GraphData]:
    """加载 graph.json，不存在或损坏返回 None。"""
    path = _resolve_path(graph_dir, GRAPH_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return GraphData.model_validate(data)
    except Exception:
        return None


def save_weights(weights: dict[str, int], graph_dir: str) -> str:
    """保存 graph_weights.json。"""
    os.makedirs(graph_dir, exist_ok=True)
    path = _resolve_path(graph_dir, WEIGHTS_FILENAME)
    payload = {"version": 1, "weights": weights}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def load_weights(graph_dir: str) -> dict[str, int]:
    """加载 graph_weights.json，不存在返回空字典。"""
    path = _resolve_path(graph_dir, WEIGHTS_FILENAME)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return dict(data.get("weights", {}))
    except Exception:
        return {}


# ---------- 极简格式导出（graph_compact.grf） ----------

# 行首标识符 + 固定列序：AI 读行首即知是什么，按位置取字段，无需理解字段名。
# 节点：id | file | function | lineno | in_degree | dynamic_weight | is_reflection
# 边：  from | to | edge_type

NODE_HEADER = "id | file | function | lineno | in_degree | dynamic_weight | is_reflection"
EDGE_HEADER = "from | to | edge_type"


def save_compact(graph: GraphData, graph_dir: str) -> str:
    """导出极简图格式 graph_compact.grf。

    设计目标：AI 读图不用反复理解字段名（行首 NODE:/EDGE: + 固定列序），
    同时保持人类可读性，方便调试。分隔符 `|` 在内容中不出现（防御性替换）。
    体积比 graph.json 小（无字段名重复、无缩进 JSON）。
    """
    os.makedirs(graph_dir, exist_ok=True)
    path = _resolve_path(graph_dir, COMPACT_FILENAME)

    def clean(v) -> str:
        return str(v).replace("|", "/")

    lines = [
        "VERSION: 1",
        "TYPE: graph",
        "SEP: |",
        "",
        f"# 节点格式：{NODE_HEADER}",
    ]
    for n in graph.nodes.values():
        lines.append(
            f"NODE: {clean(n.node_id)} | {clean(n.file)} | {clean(n.name)} | "
            f"{n.lineno} | {n.in_degree} | {n.dynamic_weight} | "
            f"{str(n.is_reflection).lower()}"
        )
    lines.append("")
    lines.append(f"# 边格式：{EDGE_HEADER}")
    for e in graph.edges:
        lines.append(f"EDGE: {clean(e.source)} | {clean(e.target)} | {e.edge_type.value}")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
