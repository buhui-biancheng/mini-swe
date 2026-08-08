# -*- coding: utf-8 -*-
"""Phase 6：CowManager — COW 工作副本管理（真实代码只读，副本可写）。"""
import os
import shutil
import tempfile

# 复制时排除的目录（大目录/隐藏目录，避免副本膨胀）
EXCLUDE_DIRS = {".git", ".graph", "venv", ".venv", "__pycache__",
                "node_modules", "third_party", ".pytest_cache", ".mypy_cache"}


class CowManager:
    """写时复制工作副本管理器。

    真实代码目录只读（绝不被 Agent 触碰）；所有编辑发生在副本
    /tmp/agent_workspace/{task_id}/，任务结束销毁。
    """

    def __init__(self, real_dir: str):
        self.real_dir = os.path.abspath(real_dir)

    @property
    def workspace_root(self) -> str:
        return os.path.join(tempfile.gettempdir(), "agent_workspace")

    def create(self, task_id: str = "default") -> str:
        """创建副本（复制真实目录 → /tmp/agent_workspace/{task_id}/）。"""
        ws = os.path.join(self.workspace_root, task_id)
        # 清理旧的
        if os.path.exists(ws):
            shutil.rmtree(ws, ignore_errors=True)
        os.makedirs(ws, exist_ok=True)

        copied = 0
        for item in os.listdir(self.real_dir):
            src = os.path.join(self.real_dir, item)
            dst = os.path.join(ws, item)
            if os.path.isdir(src):
                if item in EXCLUDE_DIRS:
                    continue
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                    ".git", ".graph", "venv", "__pycache__", "node_modules"))
            else:
                shutil.copy2(src, dst)
            copied += 1
        return ws

    def cleanup(self, task_id: str = "default") -> None:
        """销毁副本。"""
        ws = os.path.join(self.workspace_root, task_id)
        try:
            if os.path.exists(ws):
                shutil.rmtree(ws, ignore_errors=True)
        except Exception:
            pass

    def workspace(self, task_id: str = "default") -> str:
        return os.path.join(self.workspace_root, task_id)