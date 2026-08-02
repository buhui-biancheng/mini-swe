"""图索引数据模型：Node / Edge / GraphData（Pydantic 序列化）。"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class NodeType(str, Enum):
    """节点类型。"""
    FILE = "file"          # 文件节点（模块级）
    CLASS = "class"        # 类节点
    FUNCTION = "function"  # 函数/方法节点
    GLOBAL = "global"      # 全局变量节点
    RESOURCE = "resource"  # 文件操作资源节点（IO 边目标）


class EdgeType(str, Enum):
    """边类型。"""
    CALL = "call"      # 调用边：caller → callee
    DATA = "data"      # 数据边：数据源 → 消费方
    IMPORT = "import"  # 导入边：文件 → 模块文件
    INHERIT = "inherit"  # 继承边：子类 → 父类
    GLOBAL = "global"  # 全局引用边：函数 ↔ 全局变量
    IO = "io"          # 文件操作边：函数 → 资源节点


class Node(BaseModel):
    """图节点。

    node_id 统一格式：
        - 文件：      `src/main.py`
        - 函数/方法： `src/main.py::run` 或 `src/main.py::Class.method`
        - 类：        `src/main.py::Class`
        - 全局变量：  `src/config.py::CONFIG`
        - 资源：      `__io__::open`（全局唯一，不归属单文件）
    """
    node_id: str
    file: str                 # 仓库相对路径
    name: str                 # 短名（函数名/类名/变量名/资源名）
    node_type: NodeType = NodeType.FUNCTION
    lineno: int = 0
    end_lineno: int = 0
    in_degree: int = 0            # 入度：AST 静态统计
    dynamic_weight: int = 0       # 动态权重：成功修改次数（持久化）
    is_reflection: bool = False   # 是否包含反射调用


class Edge(BaseModel):
    """有向边（边不带权重，只负责导航；价值在节点）。"""
    source: str = Field(alias="from")   # 起点节点 id
    target: str = Field(alias="to")     # 终点节点 id
    edge_type: EdgeType = EdgeType.CALL

    model_config = ConfigDict(populate_by_name=True)

    def to_dict(self) -> dict:
        """序列化为 {from, to, edge_type} 格式。"""
        return {
            "from": self.source,
            "to": self.target,
            "edge_type": self.edge_type.value,
        }


class GraphMeta(BaseModel):
    """图元信息。"""
    version: int = 1
    git_commit: str = ""          # 建图时的 git HEAD
    created_at: str = ""          # 建图时间
    code_dir: str = ""            # 扫描的代码目录
    node_count: int = 0
    edge_count: int = 0
    max_in_degree: int = 0        # 所有节点最大入度（归一化用）


class GraphData(BaseModel):
    """完整图数据（graph.json 的持久化形态）。"""
    meta: GraphMeta = Field(default_factory=GraphMeta)
    nodes: dict[str, Node] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)

    def refresh_stats(self) -> None:
        """重新计算 meta 中的统计字段。"""
        self.meta.node_count = len(self.nodes)
        self.meta.edge_count = len(self.edges)
        self.meta.max_in_degree = max(
            (n.in_degree for n in self.nodes.values()), default=0
        )
