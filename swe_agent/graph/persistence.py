"""图持久化：graph.json / graph_weights.json 读写。

graph.json       — 图结构（节点 + 边 + 入度），随 git HEAD 变化重建
graph_weights.json — 动态权重（独立持久化），累积修改历史
"""

import json
import os
from typing import Optional

from .models import GraphData

WEIGHTS_FILENAME = "graph_weights.json"
GRAPH_FILENAME = "graph.json"


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
