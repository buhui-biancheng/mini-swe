"""代码依赖图 → waku 拓扑结构；工具调用 → 图节点解析。

借 waku dashboard 的 Graph tab 契约：一个 workflow = {name, nodes:[{name,kind}],
edges:[{src,dst}]}，node_start/node_end 事件按 `node` 字段点亮 SVG 节点。
我们把自已的 graph.json 转成这个形状，喂给同一个前端渲染器。

Node id 与 FSM 里的 node_id 完全一致（都来自 GraphManager(code_dir).build()），
所以事件里带的 node 一定能在拓扑图里找到。
"""

import os
import re
import threading

from swe_agent.graph.config import AgentConfig
from swe_agent.graph.manager import GraphManager


class GraphService:
    """按 code_dir 构建/缓存图索引，负责「图 → 拓扑」和「工具参数 → 节点」两个方向。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, object] = {}

    def get(self, code_dir: str):
        """构建（或缓存加载）code_dir 的图索引。"""
        key = os.path.abspath(code_dir)
        with self._lock:
            if key not in self._cache:
                mgr = GraphManager(key, config=AgentConfig())
                self._cache[key] = mgr.build()
            return self._cache[key]

    def topology(self, code_dir: str, max_nodes: int = 400) -> dict:
        """graph.json → waku 拓扑结构（附 node_count 供 UI 统计）。

        前端是「焦点+上下文」渲染：全图做淡背景（小点），当前读取节点 + 1 跳邻居
        放大带标签——所以大图不砍节点（砍了 AI 读到被砍节点就不亮灯），400 上限
        只是防御性护栏（超过按入度裁剪）。
        """
        graph = self.get(code_dir).graph
        all_nodes = list(graph.nodes.values())
        # 大图护栏：仅极端规模裁剪，平时全量供前端焦点+上下文
        if len(all_nodes) > max_nodes:
            ranked = sorted(all_nodes, key=lambda n: (-n.in_degree, len(n.node_id)))
            keep = {n.node_id for n in ranked[:max_nodes]}
            all_nodes = [n for n in all_nodes if n.node_id in keep]
        node_set = {n.node_id for n in all_nodes}
        return {
            "name": "code-graph",
            "nodes": [{"name": n.node_id, "kind": n.node_type.value} for n in all_nodes],
            "edges": [
                {"src": e.source, "dst": e.target}
                for e in graph.edges
                if e.source in node_set and e.target in node_set
            ],
            "node_count": len(all_nodes),
        }

    def context_nodes(self, code_dir: str, bug_basename: str) -> list[str]:
        """INIT 上下文节点：bug 文件内函数 + 它们的 1 跳邻居（对应 _graph_context_text）。"""
        g = self.get(code_dir)
        result: list[str] = []
        for n in g.graph.nodes.values():
            if n.node_type.value != "function":
                continue
            if os.path.basename(n.file) != bug_basename:
                continue
            result.append(n.node_id)
            neighbors = g.get_neighbors(n.node_id, hops=1)
            for depth, items in neighbors.get("neighbors", {}).items():
                for it in items:
                    node = it.get("node")
                    if node and node not in result:
                        result.append(node)
        return result

    def nodes_for_tool(self, code_dir: str, tool_name: str, args: dict | None) -> list[str]:
        """工具调用参数 → 触达的图节点 id（去重保序，上限 12 个）。"""
        g = self.get(code_dir)
        args = args or {}
        found: list[str] = []
        if tool_name in ("view_file", "edit_function"):
            fp = args.get("file_path", "")
            if fp:
                found = self._nodes_for_file(g, code_dir, fp)
        elif tool_name == "search_function":
            name = args.get("name", "")
            if name:
                found = [n.node_id for n in g.graph.nodes.values() if n.name == name][:10]
        elif tool_name in ("run_test", "run_command"):
            cmd = args.get("command", "")
            for m in re.finditer(r"([\w./\\-]+\.py)", cmd):
                p = m.group(1)
                for n in g.graph.nodes.values():
                    if n.file == p or n.file.endswith("/" + p):
                        found.append(n.node_id)
        seen: set[str] = set()
        out: list[str] = []
        for x in found:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out[:12]

    @staticmethod
    def _nodes_for_file(g, code_dir: str, fp: str) -> list[str]:
        """文件路径 → 该文件下的节点（file/class/function/global）。"""
        abs_fp = os.path.abspath(fp)
        if not os.path.exists(abs_fp):
            abs_fp = os.path.abspath(os.path.join(code_dir, fp))
        if not os.path.exists(abs_fp):
            return []
        try:
            rel = os.path.relpath(abs_fp, code_dir)
        except ValueError:
            rel = fp
        rel = rel.replace(os.sep, "/")
        out = [n.node_id for n in g.graph.nodes.values() if n.file == rel]
        if not out:
            out = [n.node_id for n in g.graph.nodes.values() if n.file.endswith("/" + rel)]
        return out
