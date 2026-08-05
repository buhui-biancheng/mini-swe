"""GraphIndex：图索引查询接口。

功能：
1. 节点/调用者/被调用者查询
2. 分层加载：L0 摘要 / L1 邻接展开 / L2 影响半径
3. 影响面计算（点权 + 距离衰减 + 去环 BFS）
4. 骨架文本生成（兼容原 SkeletonTree 输出格式）
5. search_function / expand_function（迁移工具）

点权模型：边不带权重，只负责导航；价值在节点。
"""

import os
from collections import deque
from typing import Optional

from .config import AgentConfig
from .models import Edge, EdgeType, GraphData, Node, NodeType

# 影响面遍历包含的边类型（导入边/IO 边不算调用语义）
_TRAVERSAL_EDGE_TYPES = {
    EdgeType.CALL,
    EdgeType.DATA,
    EdgeType.GLOBAL,
    EdgeType.INHERIT,
}


class GraphIndex:
    """图索引查询接口。"""

    def __init__(self, graph: GraphData, config: Optional[AgentConfig] = None,
                 logger=None):
        self.graph = graph
        self.config = config or AgentConfig()
        self._reverse: dict[str, list[Edge]] = {}   # target -> incoming edges
        self._forward: dict[str, list[Edge]] = {}   # source -> outgoing edges
        self._logger = logger
        if self._logger is None:
            try:
                from swe_agent.utils.logger import AgentLogger
                self._logger = AgentLogger()
            except Exception:
                self._logger = None
        self._build_adjacency()

    def _log_query(self, query_type: str, node: str = "", hops: int = 1) -> None:
        """记录图查询事件（DEBUG 级，供 Phase 2 调试 FSM 查询轨迹）。"""
        if self._logger is not None:
            self._logger.graph_query(query_type=query_type, node=node, hops=hops)

    # ========== 索引构建 ==========

    def _build_adjacency(self) -> None:
        self._reverse.clear()
        self._forward.clear()
        for e in self.graph.edges:
            self._forward.setdefault(e.source, []).append(e)
            self._reverse.setdefault(e.target, []).append(e)

    def reload(self, graph: GraphData) -> None:
        """替换底层图数据（增量更新后调用）。"""
        self.graph = graph
        self._build_adjacency()

    # ========== 基础查询 ==========

    def get_node(self, node_id: str) -> Optional[Node]:
        """获取单个节点。"""
        return self.graph.nodes.get(node_id)

    def in_degree_percentile(self, p: float = 95.0) -> float:
        """入度分布 p 分位数（缺陷4：阈值按图规模自适应，替代写死常数）。

        收集所有函数/类节点入度，返回排序后 p 百分位处的值；
        空图返回 0。小项目（P95≈最大值）和大项目（P95 是结构枢纽线）都自动适配。
        """
        values = sorted(
            n.in_degree for n in self.graph.nodes.values()
            if n.node_type in (NodeType.FUNCTION, NodeType.CLASS)
        )
        if not values:
            return 0.0
        k = (len(values) - 1) * (p / 100.0)
        lo = int(k)
        hi = min(lo + 1, len(values) - 1)
        frac = k - lo
        return values[lo] * (1.0 - frac) + values[hi] * frac

    def file_level_prior_text(self) -> str:
        """L-1 文件级全局先验：代码库结构地图（文件|函数数|入度|枢纽）+ 文件间调用。

        数据全部来自图（file 字段 + import/call 边 + 入度 + is_test_file），
        不新增存储；文件级是注入时的聚合视图，函数级图保留作查询索引。
        列函数名规则：总函数名 ≤50 全列；超过只保留测试文件 + 枢纽文件的函数名。
        （2026-08-05：从 AgentFSM 重构至此，FSM 与 diagnose 共用同一份数据）
        """
        g = self.graph
        nodes = list(g.nodes.values())
        edges = list(g.edges)

        file_funcs: dict = {}
        for n in nodes:
            if n.node_type.value == "function":
                file_funcs.setdefault(n.file, []).append(n)
        if not file_funcs:
            return "【图索引 L-1 文件级先验】（无函数节点）"

        file_in_degree = {f: sum(x.in_degree for x in funcs) for f, funcs in file_funcs.items()}
        file_func_count = {f: len(funcs) for f, funcs in file_funcs.items()}

        threshold = float(self.config.adaptive_impact_threshold(len(nodes)))
        hub_files = {f for f, d in file_in_degree.items() if d >= threshold}

        total_funcs = sum(file_func_count.values())
        list_all = total_funcs <= 50

        lines = ["# 代码库结构（文件级先验）: 文件 | 函数数 | 入度"]
        for f in sorted(file_funcs.keys()):
            cnt = file_func_count[f]
            deg = file_in_degree[f]
            is_hub = f in hub_files
            is_test = self.is_test_file(f)
            tag = " [枢纽]" if is_hub else ""
            test_tag = " ← 测试锚点" if is_test else ""
            func_names = ""
            if list_all or is_hub or is_test:
                func_names = f" → {', '.join(x.name for x in file_funcs[f])}"
            lines.append(f"#   {f}: {cnt} 函数, 入度 {deg}{tag}{func_names}{test_tag}")

        file_edges: dict = {}
        for e in edges:
            src = g.nodes.get(e.source)
            tgt = g.nodes.get(e.target)
            if src is None or tgt is None:
                continue
            if src.file == tgt.file:
                continue
            if e.edge_type.value in ("import", "call", "data", "global"):
                file_edges.setdefault(src.file, set()).add(tgt.file)
        if file_edges:
            lines.append("# 文件间调用/导入:")
            for f in sorted(file_edges.keys()):
                targets = " / ".join(sorted(file_edges[f]))
                lines.append(f"#   {f} → {targets}")

        return "【图索引 L-1 文件级先验】\n" + "\n".join(lines)

    def is_test_file(self, file_path: str) -> bool:
        """确定性识别测试文件（不依赖命名规范外的任何假设）。

        规则（任一命中即测试文件）：
            - 路径含 tests/ 目录段
            - 文件名 test_ 前缀（test_xxx.py）
            - 文件名 _test.py / _tests.py 后缀

        用途：影响面计算排除测试节点（测试调用不算生产影响面），
             以及 Phase 7 诊断阶段发现测试锚点。
        """
        p = file_path.replace("\\", "/")
        segs = p.split("/")
        if "tests" in segs:
            return True
        base = os.path.basename(p)
        return (
            base.startswith("test_")
            or base.endswith("_test.py")
            or base.endswith("_tests.py")
        )

    def is_test_node(self, node: "Node") -> bool:
        """节点是否属于测试文件。"""
        return self.is_test_file(node.file)

    def get_callers(self, node_id: str) -> list[Node]:
        """获取所有调用方（上游节点）。返回节点列表，边不带权重。"""
        nodes = []
        for e in self._reverse.get(node_id, []):
            if e.edge_type in _TRAVERSAL_EDGE_TYPES:
                node = self.graph.nodes.get(e.source)
                if node:
                    nodes.append(node)
        return self._dedup_nodes(nodes)

    def get_callees(self, node_id: str) -> list[Node]:
        """获取所有被调用方（下游节点）。"""
        nodes = []
        for e in self._forward.get(node_id, []):
            if e.edge_type in _TRAVERSAL_EDGE_TYPES:
                node = self.graph.nodes.get(e.target)
                if node:
                    nodes.append(node)
        return self._dedup_nodes(nodes)

    def get_edges(self, node_id: str) -> tuple[list[Edge], list[Edge]]:
        """获取节点的出边和入边（含导入/IO 边）。"""
        return self._forward.get(node_id, []), self._reverse.get(node_id, [])

    @staticmethod
    def _dedup_nodes(nodes: list[Node]) -> list[Node]:
        seen = set()
        out = []
        for n in nodes:
            if n.node_id not in seen:
                seen.add(n.node_id)
                out.append(n)
        return out

    # ========== 分层加载 ==========

    def get_summary(self, traceback_hint: Optional[str] = None) -> dict:
        """L0 摘要：节点数、边数、Top-N 高入度节点、报错命中节点。"""
        self._log_query("summary")
        nodes = sorted(
            (n for n in self.graph.nodes.values()
             if n.node_type.value != "resource" and not self.is_test_node(n)),
            key=lambda n: n.in_degree,
            reverse=True,
        )
        top = nodes[: self.config.top_n_in_degree]

        summary = {
            "node_count": self.graph.meta.node_count,
            "edge_count": self.graph.meta.edge_count,
            "file_count": sum(
                1 for n in self.graph.nodes.values()
                if n.node_type.value == "file"
            ),
            "function_count": sum(
                1 for n in self.graph.nodes.values()
                if n.node_type.value == "function"
            ),
            "top_in_degree": [
                {
                    "node": n.node_id,
                    "in_degree": n.in_degree,
                    "dynamic_weight": n.dynamic_weight,
                }
                for n in top
            ],
            "traceback_hit": self._match_traceback(traceback_hint),
        }
        return summary

    def _match_traceback(self, hint: Optional[str]) -> Optional[dict]:
        """从 Traceback 文本中匹配命中的节点。

        优先规则：行号命中函数体范围 > 函数/类节点 > 文件节点。
        """
        if not hint:
            return None
        import re

        best = None
        best_rank = -1
        for line in hint.splitlines():
            line = line.strip()
            if ".py" not in line:
                continue
            # 提取 "xxx.py" 及后面的行号（line 8 或 :8 两种格式）
            m = re.search(r'([\w./\\-]+\.py).*?(?:line\s+(\d+)|:(\d+))', line)
            if not m:
                continue
            file_part = m.group(1)
            lineno = int(m.group(2) or m.group(3) or 0)
            base = os.path.basename(file_part)

            for n in self.graph.nodes.values():
                if os.path.basename(n.file) != base:
                    continue
                rank = 0
                if n.node_type.value in ("function", "class"):
                    rank = 1
                    # 行号命中函数体范围 → 最高优先级
                    if lineno and n.lineno <= lineno <= n.end_lineno:
                        rank = 2
                if rank <= best_rank:
                    continue
                best_rank = rank
                best = {
                    "node": n.node_id,
                    "file": n.file,
                    "lineno": lineno or n.lineno,
                    "node_type": n.node_type.value,
                    "in_degree": n.in_degree,
                }
        return best

    def get_neighbors(self, node_id: str, hops: int = 1) -> dict:
        """L1/L2 邻接展开：指定跳数内的邻居。"""
        self._log_query("neighbors", node=node_id, hops=hops)
        node = self.graph.nodes.get(node_id)
        if not node:
            return {"error": f"节点不存在: {node_id}"}

        level_results: dict[int, list[dict]] = {}
        visited = {node_id}
        queue = deque([(node_id, 0)])

        while queue:
            current_id, depth = queue.popleft()
            if depth >= hops:
                continue
            neighbors = []
            for e in self._reverse.get(current_id, []) + self._forward.get(current_id, []):
                if e.edge_type not in _TRAVERSAL_EDGE_TYPES:
                    continue
                other = e.target if e.source == current_id else e.source
                if other in visited:
                    continue
                n = self.graph.nodes.get(other)
                if not n:
                    continue
                neighbors.append({
                    "node": n.node_id,
                    "node_type": n.node_type.value,
                    "in_degree": n.in_degree,
                    "edge_type": e.edge_type.value,
                    "direction": "caller" if e.target == current_id else "callee",
                })
                visited.add(other)
                queue.append((other, depth + 1))

            if neighbors:
                level_results[depth + 1] = neighbors

        return {
            "node": node_id,
            "hops": hops,
            "neighbors": level_results,
        }

    # ========== 影响面计算（去环 BFS） ==========

    def compute_impact(self, node_id: str,
                       max_hops: Optional[int] = None,
                       decay: Optional[float] = None) -> float:
        """计算影响面：从起点沿调用链向调用方/消费方延伸，点权累加。

        去环规则：每个节点在一条路径上只能贡献一次影响面。
        点权 = max(1, dynamic_weight) × in_degree_normalized × 衰减因子^跳数
        """
        self._log_query("impact", node=node_id, hops=max_hops or self.config.max_hops)
        max_hops = max_hops if max_hops is not None else self.config.max_hops
        decay = decay if decay is not None else self.config.decay
        total_cost = 0.0
        visited = {node_id}
        queue = deque([(node_id, 0)])

        while queue:
            node, hops = queue.popleft()
            if hops >= max_hops:
                continue

            for caller in self.get_callers(node):
                if caller.node_id in visited:
                    continue  # 断链，不往后算
                visited.add(caller.node_id)
                if self.is_test_node(caller):
                    continue  # 测试调用不算生产影响面

                in_degree_norm = (
                    caller.in_degree / self.graph.meta.max_in_degree
                    if self.graph.meta.max_in_degree else 0.0
                )
                history_factor = max(1, caller.dynamic_weight)  # 默认为 1，不归零
                current_decay = decay ** hops
                cost = history_factor * in_degree_norm * current_decay
                total_cost += cost

                queue.append((caller.node_id, hops + 1))

        return round(total_cost, 6)

    def compute_impact_detail(self, node_id: str,
                              max_hops: Optional[int] = None,
                              decay: Optional[float] = None) -> dict:
        """影响面计算（含明细，供日志/演示）。"""
        self._log_query("impact_detail", node=node_id, hops=max_hops or self.config.max_hops)
        max_hops = max_hops if max_hops is not None else self.config.max_hops
        decay = decay if decay is not None else self.config.decay
        total = 0.0
        details = []
        visited = {node_id}
        queue = deque([(node_id, 0)])

        while queue:
            node, hops = queue.popleft()
            if hops >= max_hops:
                continue
            for caller in self.get_callers(node):
                if caller.node_id in visited:
                    continue
                visited.add(caller.node_id)
                if self.is_test_node(caller):
                    continue  # 测试调用不算生产影响面
                norm = (caller.in_degree / self.graph.meta.max_in_degree
                        if self.graph.meta.max_in_degree else 0.0)
                hist = max(1, caller.dynamic_weight)
                dec = decay ** hops
                cost = hist * norm * dec
                total += cost
                details.append({
                    "node": caller.node_id,
                    "hops": hops + 1,
                    "history_factor": hist,
                    "in_degree_norm": round(norm, 6),
                    "decay": round(dec, 6),
                    "cost": round(cost, 6),
                })
                queue.append((caller.node_id, hops + 1))

        return {
            "start": node_id,
            "total_cost": round(total, 6),
            "affected_nodes": len(details),
            "max_hops": max_hops,
            "decay": decay,
            "details": details,
        }

    # ========== 骨架兼容层 ==========

    def generate_skeleton_text(self) -> str:
        """生成骨架文本（兼容原 SkeletonTree 输出格式）。"""
        files: dict[str, list[Node]] = {}
        for n in self.graph.nodes.values():
            if n.node_type.value not in ("function", "class"):
                continue
            files.setdefault(n.file, []).append(n)

        parts = []
        for file_path in sorted(files.keys()):
            funcs = sorted(files[file_path], key=lambda n: n.lineno)
            lines = [f"=== {file_path} ==="]
            for func in funcs:
                if func.node_type.value == "class":
                    lines.append(f"  {func.name} (class, lines {func.lineno}-{func.end_lineno})")
                else:
                    lines.append(f"  {func.name} (lines {func.lineno}-{func.end_lineno})")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)

    def search_nodes(self, name: str, limit: int = 30) -> list[Node]:
        """搜索函数（模糊匹配）。"""
        name_lower = name.lower()
        results = []
        for n in self.graph.nodes.values():
            if n.node_type.value not in ("function", "class"):
                continue
            if name_lower in n.name.lower() or name_lower in n.node_id.lower():
                results.append(n)
                if len(results) >= limit:
                    break
        return results

    def search_function(self, name: str) -> list[dict]:
        """搜索函数（兼容工具层返回格式）。"""
        return [
            {
                "node": n.node_id,
                "name": n.name,
                "file": n.file,
                "lines": f"{n.lineno}-{n.end_lineno}",
                "in_degree": n.in_degree,
            }
            for n in self.search_nodes(name)
        ]

    def expand_function(self, file_path: str, func_name: str) -> Optional[str]:
        """展开指定函数的完整源码（从节点行号范围提取）。"""
        node = self._find_node(file_path, func_name)
        if not node:
            return None
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            abs_path = os.path.join(self.graph.meta.code_dir, node.file)
        if not os.path.exists(abs_path):
            return None
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            return None
        return "".join(lines[node.lineno - 1: node.end_lineno])

    def get_file_functions(self, file_path: str) -> list[Node]:
        """获取指定文件的所有函数/方法节点（兼容原 SkeletonTree 接口，不含类）。"""
        result = []
        base = os.path.basename(file_path)
        for n in self.graph.nodes.values():
            if n.node_type.value != "function":
                continue
            if file_path == n.file or os.path.basename(n.file) == base:
                result.append(n)
        return result

    def find_node(self, file_path: str, func_name: str) -> Optional[Node]:
        """按文件+函数名定位节点（公开接口，供 view_file 三模式使用）。"""
        candidates = []
        for n in self.graph.nodes.values():
            if n.node_type.value not in ("function", "class"):
                continue
            if n.name != func_name:
                continue
            base = os.path.basename(n.file)
            file_base = os.path.basename(file_path)
            if file_path == n.file or base == file_base or file_path in n.file:
                candidates.append(n)
        if not candidates:
            return None
        # 精确路径匹配优先
        for c in candidates:
            if c.file == file_path or c.file.endswith(file_path):
                return c
        return candidates[0]

    def resolve_location(self, file_path: str, lineno: int) -> Optional[Node]:
        """按文件+行号匹配节点（Phase 2 报错坐标 → 图节点）。

        优先级：行号命中函数体 > 函数/类 > 文件节点。
        """
        best = None
        best_rank = -1
        base = os.path.basename(file_path)
        for n in self.graph.nodes.values():
            if os.path.basename(n.file) != base:
                continue
            rank = 0
            if n.node_type.value in ("function", "class"):
                rank = 1
                if lineno and n.lineno <= lineno <= n.end_lineno:
                    rank = 2
            if rank <= best_rank:
                continue
            best_rank = rank
            best = n
        return best

    # 兼容旧名（_find_node 已公开为 find_node）
    def _find_node(self, file_path: str, func_name: str) -> Optional[Node]:
        return self.find_node(file_path, func_name)
