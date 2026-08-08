# -*- coding: utf-8 -*-
"""Phase 4 快照系统：SnapshotManager（代码 + 图权重 + FSM 状态）。"""
from __future__ import annotations
import json, os, shutil, tempfile, time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Snapshot:
    index: int
    timestamp: float
    files: dict = field(default_factory=dict)
    weights: dict = field(default_factory=dict)
    fsm_state: dict = field(default_factory=dict)
    milestone: bool = False

class SnapshotManager:
    def __init__(self, code_dir: str, task_id: str = "default",
                 max_snapshots: int = 5, verbose: bool = False):
        self.code_dir = os.path.abspath(code_dir)
        self.task_id = task_id
        self.max_snapshots = max_snapshots
        self.verbose = verbose
        self.base_dir = os.path.join(tempfile.gettempdir(), "agent_workspace", f"{task_id}_snapshots")
        self.snapshots: list[Snapshot] = []
        self._counter = 0

    def save(self, files: dict, weights: Optional[dict] = None,
             fsm_state: Optional[dict] = None, milestone: bool = False) -> int:
        self._counter += 1
        snap = Snapshot(index=self._counter, timestamp=time.time(),
                        files=dict(files), weights=dict(weights or {}),
                        fsm_state=dict(fsm_state or {}), milestone=milestone)
        self.snapshots.append(snap)
        self._prune()
        self._persist(snap)
        if self.verbose:
            print(f"[SNAPSHOT] 保存 #{snap.index} files={len(snap.files)} weights={len(snap.weights)}{' [里程碑]' if milestone else ''}")
        return snap.index

    def _prune(self) -> None:
        if len(self.snapshots) <= self.max_snapshots:
            return
        for i, s in enumerate(self.snapshots):
            if not s.milestone:
                del self.snapshots[i]
                break

    def _persist(self, snap: Snapshot) -> None:
        try:
            snap_dir = os.path.join(self.base_dir, f"snap_{snap.index:03d}")
            os.makedirs(os.path.join(snap_dir, "code"), exist_ok=True)
            for path, content in snap.files.items():
                safe = os.path.basename(path).replace("/", "_").replace("\\", "_")
                with open(os.path.join(snap_dir, "code", safe), "w", encoding="utf-8") as f:
                    f.write(content)
            with open(os.path.join(snap_dir, "weights.json"), "w", encoding="utf-8") as f:
                json.dump(snap.weights, f, ensure_ascii=False, indent=2)
            with open(os.path.join(snap_dir, "fsm_state.json"), "w", encoding="utf-8") as f:
                json.dump(snap.fsm_state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if self.verbose:
                print(f"[SNAPSHOT] 落盘失败: {e}")

    def latest(self) -> Optional[Snapshot]:
        return self.snapshots[-1] if self.snapshots else None

    def initial(self) -> Optional[Snapshot]:
        for s in self.snapshots:
            if not s.milestone:
                return s
        return self.snapshots[0] if self.snapshots else None

    def restore_files(self, index: int = -1) -> int:
        snap = self.snapshots[index] if index < len(self.snapshots) else None
        if snap is None:
            return 0
        restored = 0
        for path, content in snap.files.items():
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                restored += 1
            except Exception:
                continue
        if self.verbose:
            print(f"[SNAPSHOT] 恢复 #{snap.index}: {restored} 个文件")
        return restored

    def restore_weights(self, index: int = -1) -> dict:
        snap = self.snapshots[index] if index < len(self.snapshots) else None
        if snap is None:
            return {}
        if self.verbose:
            print(f"[SNAPSHOT] 恢复权重 #{snap.index}: {len(snap.weights)} 条")
        return dict(snap.weights)

    def clear(self) -> None:
        self.snapshots.clear()

    def cleanup_all(self) -> None:
        self.clear()
        try:
            if os.path.exists(self.base_dir):
                shutil.rmtree(self.base_dir, ignore_errors=True)
        except Exception:
            pass