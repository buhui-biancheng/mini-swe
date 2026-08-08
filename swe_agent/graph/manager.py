"""GraphManager：图生命周期管理（构建 / 增量 / 权重 / 缓存 统一入口）。

所有访问方（FSM、工具、CLI、JIT 补全）都通过 GraphManager 访问图，
不直接操作 graph.json。

缓存策略：
    graph.json 存在 AND meta.git_commit == 当前 git HEAD → 直接加载
    graph.json 存在 BUT git HEAD 变了             → git diff 增量更新
    graph.json 不存在                             → 全量 AST 扫描
"""

import os
import subprocess
import time
from typing import Optional

from .builder import GraphBuilder
from .config import AgentConfig
from .index import GraphIndex
from .models import Edge, EdgeType, GraphData
from . import persistence


class GraphManager:
    """图生命周期管理器。"""

    def __init__(
        self,
        code_dir: str,
        config: Optional[AgentConfig] = None,
        graph_dir: Optional[str] = None,
    ):
        self.code_dir = os.path.abspath(code_dir)
        self.config = config or AgentConfig()
        self.graph_dir = graph_dir or os.path.join(self.code_dir, ".graph")
        self.builder = GraphBuilder(self.code_dir, self.config)
        self._index: Optional[GraphIndex] = None
        self._weights: dict[str, int] = {}
        self._logger = None
        try:
            from swe_agent.utils.logger import AgentLogger
            self._logger = AgentLogger()
        except Exception:
            self._logger = None

    def _log_build(self, mode: str, graph: GraphData) -> None:
        """记录图构建日志（模式：full / cache / incremental）。"""
        if self._logger is None:
            return
        self._logger.graph_build(
            mode=mode,
            node_count=graph.meta.node_count,
            edge_count=graph.meta.edge_count,
        )

    # ========== 构建 ==========

    def build(self, force: bool = False) -> GraphIndex:
        """构建/加载图索引（含缓存逻辑）。"""
        start = time.time()
        if not force:
            cached = self._load_cached_graph()
            if cached is not None:
                self._index = GraphIndex(cached, self.config)
                self._merge_weights()
                self._log_build("cache", cached)
                return self._index

        # 尝试增量更新（确保 builder 有全量符号表，否则增量不完整）
        if not force:
            old = persistence.load_graph(self.graph_dir)
            if old is not None and old.meta.git_commit:
                changed = self._git_changed_files(old.meta.git_commit)
                if changed:
                    self._ensure_builder_loaded()
                    new_graph = self.builder.update(changed)
                    if new_graph is not None:
                        new_graph.meta.created_at = old.meta.created_at
                        self._persist(new_graph)
                        self._index = GraphIndex(new_graph, self.config)
                        self._merge_weights()
                        self._log_build("incremental", new_graph)
                        return self._index

        # 全量扫描
        graph = self.builder.build()
        self._persist(graph)
        self._index = GraphIndex(graph, self.config)
        self._merge_weights()
        self._log_build("full", graph)
        return self._index

    def get_index(self) -> GraphIndex:
        """只读查询入口（未构建时自动构建）。"""
        if self._index is None:
            self.build()
        return self._index

    def force_rebuild(self) -> GraphIndex:
        """强制全量重建。"""
        return self.build(force=True)

    def _ensure_builder_loaded(self) -> None:
        """确保 Builder 持有全量符号表（缓存加载后增量更新需要前置条件）。

        场景：进程从缓存加载图（builder 未扫描过），此时若直接 update_from_diff
        会导致 builder 只有变更文件，图不完整。必须先全量扫描填充符号表。
        """
        if not self.builder.is_loaded:
            self.builder.build()

    # ========== 增量更新 ==========

    def update_from_diff(self, changed_files: list[str]) -> GraphIndex:
        """增量更新：AST 重新扫描变更文件。"""
        self._ensure_builder_loaded()
        graph = self.builder.update(changed_files)
        if graph is not None:
            self._persist(graph)
            self._index = GraphIndex(graph, self.config)
            self._merge_weights()
        return self._index

    # ========== 权重持久化 ==========

    def load_weights(self) -> dict[str, int]:
        """从 graph_weights.json 加载权重。"""
        self._weights = persistence.load_weights(self.graph_dir)
        return self._weights

    def save_weights(self) -> None:
        """持久化动态权重到 graph_weights.json。"""
        persistence.save_weights(self._weights, self.graph_dir)

    def get_weight(self, node_id: str) -> int:
        """获取节点成功计数（未加载则自动加载）。"""
        if not self._weights:
            self.load_weights()
        return self._weights.get(node_id, {}).get("success", 0)

    def get_fail_count(self, node_id: str) -> int:
        """获取节点失败计数（缺陷3）。"""
        if not self._weights:
            self.load_weights()
        return self._weights.get(node_id, {}).get("fail", 0)

    def update_dynamic_weight(self, node_id: str) -> None:
        """修复成功后，节点 success_count +1（并持久化）。"""
        if not self._weights:
            self.load_weights()
        entry = self._weights.get(node_id, {"success": 0, "fail": 0})
        entry["success"] = entry.get("success", 0) + 1
        self._weights[node_id] = entry
        self.save_weights()
        # 同步到当前图
        if self._index is not None:
            node = self._index.graph.nodes.get(node_id)
            if node:
                node.dynamic_weight = entry["success"]
                node.fail_count = entry.get("fail", 0)

    def record_failure(self, node_id: str) -> None:
        """修复失败/回滚后，节点 fail_count +1（缺陷3）。"""
        if not self._weights:
            self.load_weights()
        entry = self._weights.get(node_id, {"success": 0, "fail": 0})
        entry["fail"] = entry.get("fail", 0) + 1
        self._weights[node_id] = entry
        self.save_weights()
        if self._index is not None:
            node = self._index.graph.nodes.get(node_id)
            if node:
                node.fail_count = entry["fail"]

    def apply_jit_update(self, node_id: str, target: str,
                        edge_type: str, evidence: str) -> dict:
        """JIT 图谱补全（Phase 5）：验证 + 写入 + 持久化。

        四层防幻觉防线：
            1. 证据绑定：evidence 非空
            2. 系统验证：源节点是反射节点 + 目标节点真实存在
            3. 文件绑定：源节点 is_reflection=true（只补反射节点）
            4. 确定性规则：目标不存在 / 边已存在 / 自环 → 拒绝

        Returns:
            {"accepted": bool, "reason": str}
        """
        g = self._index.graph if self._index else None
        if g is None:
            return {"accepted": False, "reason": "图未加载"}
        # 防线 0（幂等优先）：边已存在 → 拒绝（在反射检查之前，已解析的节点重复提交也能正确拒绝）
        for e in g.edges:
            if e.source == node_id and e.target == target:
                return {"accepted": False, "reason": "该边已存在（幂等）"}
        # 防线 4b：不允许自环
        if node_id == target:
            return {"accepted": False, "reason": "自环补全拒绝"}
        # 防线 2：源节点存在且是反射节点
        src = g.nodes.get(node_id)
        if src is None:
            return {"accepted": False, "reason": f"源节点不存在: {node_id}"}
        if not src.is_reflection:
            return {"accepted": False, "reason": f"源节点非反射节点: {node_id}"}
        # 防线 4a：目标存在
        tgt = g.nodes.get(target)
        if tgt is None:
            return {"accepted": False, "reason": f"目标节点不存在: {target}"}
        # 防线 4c：非法边类型
        try:
            et = EdgeType(edge_type)
        except ValueError:
            return {"accepted": False, "reason": f"非法边类型: {edge_type}"}
        # 防线 1：证据非空
        if not evidence or len(evidence.strip()) < 5:
            return {"accepted": False, "reason": "证据不足（<5 字符）"}

        # 写入：新增边 + 标签 resolved + 持久化
        g.edges.append(Edge(source=node_id, target=target, edge_type=et))
        src.is_reflection = False  # 已解析（原 true → 不再标记）
        self._persist(g)
        return {"accepted": True, "reason": "已写入", "edge": f"{node_id} -> {target}"}

    def get_weights_snapshot(self) -> dict:
        """返回权重完整快照（Phase 4 机制四：回退用）。"""
        if not self._weights:
            self.load_weights()
        return dict(self._weights)

    def restore_weights_snapshot(self, weights: dict) -> None:
        """从快照恢复权重并持久化（Phase 4 机制四：权重回退）。"""
        self._weights = dict(weights)
        self.save_weights()
        self._merge_weights()

    def _merge_weights(self) -> None:
        """把持久化权重合并到当前图节点。"""
        if not self._weights:
            self.load_weights()
        if self._index is None:
            return
        for node_id, w in self._weights.items():
            node = self._index.graph.nodes.get(node_id)
            if node:
                node.dynamic_weight = w.get("success", 0)
                node.fail_count = w.get("fail", 0)

    # ========== 内部 ==========

    def _persist(self, graph: GraphData) -> None:
        persistence.save_graph(graph, self.graph_dir)
        if self._weights:
            persistence.save_weights(self._weights, self.graph_dir)

    def _load_cached_graph(self) -> Optional[GraphData]:
        """缓存加载：git HEAD 未变时复用。"""
        graph = persistence.load_graph(self.graph_dir)
        if graph is None:
            return None
        if not graph.meta.git_commit:
            return None
        head = self._git_head()
        if head and head == graph.meta.git_commit:
            return graph
        return None

    def _git_head(self) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", self.code_dir, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    def _git_changed_files(self, old_commit: str) -> list[str]:
        """git diff 旧 commit 与当前 HEAD 的变更文件。"""
        try:
            r = subprocess.run(
                ["git", "-C", self.code_dir, "diff", "--name-only",
                 old_commit, "HEAD", "--", "*.py"],
                capture_output=True, text=True, timeout=10,
            )
            files = [f for f in r.stdout.splitlines() if f.strip()]
            # 加上未提交的改动
            r2 = subprocess.run(
                ["git", "-C", self.code_dir, "diff", "--name-only", "--", "*.py"],
                capture_output=True, text=True, timeout=10,
            )
            files += [f for f in r2.stdout.splitlines() if f.strip()]
            return list(dict.fromkeys(files))
        except Exception:
            return []
