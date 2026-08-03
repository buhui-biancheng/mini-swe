"""加权图索引模块（Phase 1 基础设施 + Phase 2 FSM 增强模块）。"""

from .config import AgentConfig
from .builder import GraphBuilder
from .index import GraphIndex
from .manager import GraphManager
from .models import Edge, EdgeType, GraphData, GraphMeta, Node, NodeType
from .syntax_firewall import SyntaxFirewall, SyntaxCheckResult, SyntaxErrorInfo
from .traceback_parser import (
    TracebackFrame,
    TracebackResult,
    extract_frames,
    parse_traceback,
    split_sections,
    to_project_rel,
)
from .log_parser import (
    GroupedError,
    TestLogResult,
    parse_test_log,
    save_full_log,
)
from .permission import FenceCheckResult, PermissionFence
from .impact import compute_edited_impact, resolve_edited_nodes

__all__ = [
    "AgentConfig",
    "GraphBuilder",
    "GraphIndex",
    "GraphManager",
    "SyntaxFirewall",
    "SyntaxCheckResult",
    "SyntaxErrorInfo",
    # Phase 2 模块
    "TracebackFrame",
    "TracebackResult",
    "extract_frames",
    "parse_traceback",
    "split_sections",
    "to_project_rel",
    "GroupedError",
    "TestLogResult",
    "parse_test_log",
    "save_full_log",
    "FenceCheckResult",
    "PermissionFence",
    "compute_edited_impact",
    "resolve_edited_nodes",
    "Edge",
    "EdgeType",
    "GraphData",
    "GraphMeta",
    "Node",
    "NodeType",
]
