"""权限围栏：动态文件级软约束（Phase 2 模块 B）。

FSM 启动时根据图索引的确定性规则生成文件级围栏：
    - 文件内函数/类入度总和 > 阈值 → 高影响文件（核心枢纽）
    - 文件路径含 /core/ → 核心目录保护

统一为软约束：不硬拦截。命中高影响文件时：
    1. 向 AI 返回警告（"这是核心枢纽，影响面广"）
    2. 影响面计算中加入代价惩罚（penalty 乘数，引导 AI 选低代价路径）
Traceback 精确指向的文件永远允许修改（bug 所在文件必须能修）。

确定性保证：纯图统计 + 路径规则，无外部依赖、无网络。
"""

import os
from dataclasses import dataclass, field

from .config import AgentConfig
from .models import NodeType


@dataclass
class FenceCheckResult:
    """围栏检查结果（软约束：始终 allowed）。"""
    allowed: bool
    warnings: list
    penalty: float = 1.0


class PermissionFence:
    """文件级权限围栏。"""

    def __init__(self, graph_index, config=None):
        self.graph_index = graph_index
        self.config = config or AgentConfig()
        self._high_risk_files: set = set()
        self._build()

    def _build(self) -> None:
        """确定性规则生成高影响文件集合。

        缺陷4（2026-08-05）：阈值从写死常数改为图入度分布 P95 分位数，
        小项目大项目都自动适配；同时保留 max(下限) 语义防止过小项目全部命中。
        """
        file_risk: dict[str, int] = {}
        for n in self.graph_index.graph.nodes.values():
            if n.node_type in (NodeType.FUNCTION, NodeType.CLASS):
                file_risk[n.file] = file_risk.get(n.file, 0) + n.in_degree
        # 自适应阈值：max(配置下限, P95 分位数)
        p95 = self.graph_index.in_degree_percentile(95.0)
        threshold = max(float(self.config.in_degree_threshold) * 0.5, p95)
        for file_path, risk in file_risk.items():
            if risk >= threshold:
                self._high_risk_files.add(file_path)
        # 核心目录保护（路径中任一目录段为 core）
        for file_path in self.graph_index.graph.nodes:
            segs = file_path.replace("\\", "/").split("/")
            if "core" in segs[:-1]:
                self._high_risk_files.add(file_path)

    def is_high_risk(self, file_path: str) -> bool:
        """判断文件是否高风险（路径匹配：相等 / 后缀 / 同名文件）。"""
        p = file_path.replace("\\", "/")
        base = os.path.basename(p)
        for f in self._high_risk_files:
            if p == f or p.endswith("/" + f):
                return True
            if base and os.path.basename(f) == base:
                return True
        return False

    def check_edit(self, file_path: str) -> FenceCheckResult:
        """编辑前检查（软约束：返回警告 + 惩罚，不拦截）。"""
        warnings = []
        if self.is_high_risk(file_path):
            warnings.append(
                f"⚠️ {file_path} 是核心枢纽文件（入度总和/核心目录），改动影响面广，"
                f"请优先考虑低代价路径。"
            )
        return FenceCheckResult(
            allowed=True,
            warnings=warnings,
            penalty=self.config.fence_penalty if warnings else 1.0,
        )

    def penalty(self, node_id: str) -> float:
        """影响面代价惩罚：节点所属文件是高风险 → penalty 乘数。"""
        node = self.graph_index.get_node(node_id)
        if node is not None and self.is_high_risk(node.file):
            return self.config.fence_penalty
        return 1.0

    def fence_text(self) -> str:
        """围栏摘要（注入提示词，引导 AI 减少代价）。"""
        if not self._high_risk_files:
            return ""
        lines = [
            "【权限围栏】以下文件是核心枢纽（软约束：尽量少改，改前评估影响面）:"
        ]
        for f in sorted(self._high_risk_files):
            lines.append(f"  - {f}")
        return "\n".join(lines)
