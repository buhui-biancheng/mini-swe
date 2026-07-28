"""Python 适配器实现。

未来 TS 版换成 child_process + fs。
"""

import os
import json
import time
import signal
import subprocess
from pathlib import Path
from typing import Optional

from .interfaces import IProcessManager, IHeartbeatStore, ICheckpointStore


class PythonProcessManager(IProcessManager):
    """Python 进程管理器。"""

    def __init__(self, agent_script_path: str, heartbeat_file: str):
        self.script = agent_script_path
        self.heartbeat_file = heartbeat_file
        self.proc: Optional[subprocess.Popen] = None

    def start(self, resume_checkpoint: Optional[str] = None) -> None:
        """启动进程。"""
        cmd = ["python3", self.script]
        if resume_checkpoint and os.path.exists(resume_checkpoint):
            cmd += ["--resume", resume_checkpoint]

        # start_new_session=True 使其成为进程组组长
        self.proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # 删除旧心跳
        self._clear_heartbeat()

    def _clear_heartbeat(self) -> None:
        """清除旧心跳文件。"""
        try:
            os.remove(self.heartbeat_file)
        except FileNotFoundError:
            pass

    def is_alive(self) -> bool:
        """进程是否存活。"""
        return self.proc is not None and self.proc.poll() is None

    def terminate(self, graceful: bool = True) -> None:
        """终止进程。"""
        if not self.is_alive():
            return

        if graceful:
            self.proc.terminate()  # SIGTERM
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        if self.is_alive():
            # 强杀整个进程组
            self.kill_whole_group()

        self.proc = None

    def kill_whole_group(self) -> None:
        """杀死整个进程组。"""
        if self.proc and self.proc.pid:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def get_pid(self) -> int:
        """获取进程 ID。"""
        return self.proc.pid if self.proc else -1


class FileHeartbeatStore(IHeartbeatStore):
    """基于文件的心跳存储。"""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)

    def read(self) -> Optional[dict]:
        """读取心跳数据。"""
        if not self.filepath.exists():
            return None
        try:
            with open(self.filepath, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def write(self, data: dict) -> None:
        """写入心跳数据（原子写入）。"""
        tmp = self.filepath.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.rename(tmp, self.filepath)

    def clear(self) -> None:
        """清除心跳数据。"""
        try:
            self.filepath.unlink()
        except FileNotFoundError:
            pass


class FileCheckpointStore(ICheckpointStore):
    """基于文件的检查点存储。"""

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)

    def save(self, data: dict) -> None:
        """保存检查点。"""
        with open(self.filepath, "w") as f:
            json.dump(data, f, indent=2)

    def load(self) -> Optional[dict]:
        """加载检查点。"""
        if not self.filepath.exists():
            return None
        try:
            with open(self.filepath, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def exists(self) -> bool:
        """检查点是否存在。"""
        return self.filepath.exists()
