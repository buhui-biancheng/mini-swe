# -*- coding: utf-8 -*-
"""Phase 6：L1Sandbox / L2Sandbox — 两层沙盒封装。"""
import os
import shutil
import tempfile

from .cow_manager import CowManager
from .docker_runner import run_in_docker, ExecutionResult


class L1Sandbox:
    """L1 外层沙盒：Agent 所有操作（读/写/命令）都在工作副本上。

    真实代码目录绝对只读——Agent 的 registry code_dir 指向副本，
    图也建在副本的 .graph/ 里，真实代码零接触。
    """

    def __init__(self, real_dir: str):
        self.cow = CowManager(real_dir)
        self.real_dir = os.path.abspath(real_dir)
        self.workspace_dir: str = ""

    def create(self, task_id: str = "default") -> str:
        """创建 COW 工作副本，返回副本路径（= L1 沙盒世界）。"""
        self.task_id = task_id
        self.workspace_dir = self.cow.create(task_id)
        return self.workspace_dir

    def map_to_workspace(self, real_path: str) -> str:
        """把真实代码路径映射到副本路径（bug_file 转换用）。"""
        abs_path = os.path.abspath(real_path)
        if abs_path.startswith(self.real_dir):
            rel = os.path.relpath(abs_path, self.real_dir)
            return os.path.join(self.workspace_dir, rel)
        return abs_path  # 不在真实目录内的路径原样返回

    def cleanup(self) -> None:
        if self.workspace_dir and os.path.exists(self.workspace_dir):
            shutil.rmtree(self.workspace_dir, ignore_errors=True)
        self.workspace_dir = ""


class L2Sandbox:
    """L2 内层沙盒：独立 pytest 容器（每次新建 + 只读挂载 + 用完销毁）。

    复用 docker_runner.run_in_docker（已是：新容器 / read_only / 断网 /
    tmpfs /tmp / 自动销毁）。L2 保证测试环境与 Agent 环境隔离，
    且测试容器无法修改工作副本（只读挂载）。
    """

    @staticmethod
    def run_test(code_dir: str, command: str, timeout: int = 60) -> ExecutionResult:
        """在独立容器中跑测试（code_dir 应为 L1 工作副本）。"""
        return run_in_docker(code_dir, command, timeout=timeout)