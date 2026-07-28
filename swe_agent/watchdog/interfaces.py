"""抽象接口：适配层。

Python 版用 subprocess/os 实现，TS 版用 child_process 实现。
"""

from abc import ABC, abstractmethod
from typing import Optional


class IProcessManager(ABC):
    """进程管理器接口。"""

    @abstractmethod
    def start(self, resume_checkpoint: Optional[str] = None) -> None:
        """启动进程。"""
        ...

    @abstractmethod
    def is_alive(self) -> bool:
        """进程是否存活。"""
        ...

    @abstractmethod
    def terminate(self, graceful: bool = True) -> None:
        """终止进程。"""
        ...

    @abstractmethod
    def kill_whole_group(self) -> None:
        """杀死整个进程组。"""
        ...

    @abstractmethod
    def get_pid(self) -> int:
        """获取进程 ID。"""
        ...


class IHeartbeatStore(ABC):
    """心跳存储接口。"""

    @abstractmethod
    def read(self) -> Optional[dict]:
        """读取心跳数据。"""
        ...

    @abstractmethod
    def write(self, data: dict) -> None:
        """写入心跳数据。"""
        ...

    @abstractmethod
    def clear(self) -> None:
        """清除心跳数据。"""
        ...


class ICheckpointStore(ABC):
    """检查点存储接口。"""

    @abstractmethod
    def save(self, data: dict) -> None:
        """保存检查点。"""
        ...

    @abstractmethod
    def load(self) -> Optional[dict]:
        """加载检查点。"""
        ...

    @abstractmethod
    def exists(self) -> bool:
        """检查点是否存在。"""
        ...
