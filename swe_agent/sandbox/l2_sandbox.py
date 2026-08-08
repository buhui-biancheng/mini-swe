# -*- coding: utf-8 -*-
"""Phase 6：L2Sandbox — 内层沙盒（独立 pytest 容器）。

复用 docker_runner.run_in_docker：每次新容器 / 只读挂载 / 断网 /
tmpfs /tmp / 用完自动销毁。测试容器无法修改工作副本。
"""

from .docker_runner import run_in_docker, ExecutionResult


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