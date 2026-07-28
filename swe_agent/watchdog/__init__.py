"""看门狗模块：三层架构实现。

架构：
1. 纯决策引擎 (DecisionEngine) - 无外部依赖，纯逻辑
2. 抽象适配层 (IProcessManager, IHeartbeatStore, ICheckpointStore)
3. 监控主循环 (WatchdogOrchestrator)
"""

from .config import WatchdogConfig
from .decision_engine import DecisionEngine, Action, HealthStatus
from .interfaces import IProcessManager, IHeartbeatStore, ICheckpointStore
from .python_adapter import PythonProcessManager, FileHeartbeatStore, FileCheckpointStore
from .orchestrator import WatchdogOrchestrator

__all__ = [
    "WatchdogConfig",
    "DecisionEngine",
    "Action",
    "HealthStatus",
    "IProcessManager",
    "IHeartbeatStore",
    "ICheckpointStore",
    "PythonProcessManager",
    "FileHeartbeatStore",
    "FileCheckpointStore",
    "WatchdogOrchestrator",
]
