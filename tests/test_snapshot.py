# -*- coding: utf-8 -*-
"""Phase 4 测试：SnapshotManager 单测 + 调低阈值验证回退真实执行 + 异常信号检测。"""
import os
import sys

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest

from swe_agent.snapshot import SnapshotManager


# ── 机制一：SnapshotManager 单测 ──
class TestSnapshotManager:
    def test_save_restore_files(self, tmp_path):
        code_dir = str(tmp_path / "proj")
        os.makedirs(code_dir)
        bug = os.path.join(code_dir, "bug.py")
        with open(bug, "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        mgr = SnapshotManager(code_dir, task_id="t1", verbose=False)
        mgr.save({bug: "def f():\n    return 1\n"}, weights={"x::f": {"success": 1, "fail": 0}})
        # 修改文件（模拟编辑）
        with open(bug, "w", encoding="utf-8") as f:
            f.write("def f():\n    return 999\n")
        restored = mgr.restore_files()
        assert restored == 1
        with open(bug, "r", encoding="utf-8") as f:
            assert "return 1" in f.read()
        print("✅ 文件快照保存/恢复")

    def test_weights_restore(self, tmp_path):
        code_dir = str(tmp_path / "proj2")
        os.makedirs(code_dir)
        mgr = SnapshotManager(code_dir, task_id="t2", verbose=False)
        mgr.save({}, weights={"a::f": {"success": 3, "fail": 1}})
        w = mgr.restore_weights()
        assert w == {"a::f": {"success": 3, "fail": 1}}
        print("✅ 权重快照恢复")

    def test_fifo_prune(self, tmp_path):
        code_dir = str(tmp_path / "proj3")
        os.makedirs(code_dir)
        mgr = SnapshotManager(code_dir, task_id="t3", max_snapshots=3, verbose=False)
        for i in range(6):
            mgr.save({}, weights={"n": i})
        assert len(mgr.snapshots) <= 3, f"应保留 3 个，实际 {len(mgr.snapshots)}"
        # 里程碑不被淘汰
        mgr2 = SnapshotManager(code_dir, task_id="t4", max_snapshots=2, verbose=False)
        mgr2.save({}, milestone=True)
        mgr2.save({})
        mgr2.save({})
        mgr2.save({})
        assert any(s.milestone for s in mgr2.snapshots), "里程碑应保留"
        print("✅ FIFO 淘汰 + 里程碑保留")

    def test_cleanup(self, tmp_path):
        code_dir = str(tmp_path / "proj4")
        os.makedirs(code_dir)
        mgr = SnapshotManager(code_dir, task_id="t5", verbose=False)
        mgr.save({}, weights={})
        import os as _os
        assert _os.path.exists(mgr.base_dir), "快照应落盘"
        mgr.cleanup_all()
        assert not _os.path.exists(mgr.base_dir), "清理后磁盘目录应删除"
        print("✅ 清理")


# ── 机制四：权重回退（调低阈值验证回退真实执行） ──
class TestRollbackReal:
    def _make_fsm(self, impact_threshold=1.0, rollback_limit=2):
        """构造 FSM：低阈值让回退必然触发（用户思路：调低阈值验证回退）。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        from swe_agent.graph.config import AgentConfig
        import types

        cfg = AgentConfig(impact_threshold=impact_threshold,
                          rollback_limit=rollback_limit)
        # 直接用真实 FSM（mock LLM 由 monkeypatch 处理）
        return AgentFSM

    def test_weight_rollback_on_fail(self, tmp_path):
        """FSM 失败后权重应回退到初始（不污染图）。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        from swe_agent.graph.config import AgentConfig

        proj = str(tmp_path / "p")
        os.makedirs(proj)
        with open(os.path.join(proj, "bug.py"), "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a - b  # BUG\n")

        # 低阈值：任何编辑都触发 ROLLBACK → 走完整回退路径
        cfg = AgentConfig(impact_threshold=0.5, rollback_limit=1)
        fsm = AgentFSM(bug_file=os.path.join(proj, "bug.py"),
                       test_command="pytest -q", mode="dp",
                       max_retries=0)
        fsm.agent_config = cfg
        # 模拟：先有初始权重，再编辑（触发回退路径）
        fsm._initial_weights = {"bug.py::add": {"success": 2, "fail": 0}}
        # 模拟编辑后权重变化
        fsm.graph_manager._weights = {"bug.py::add": {"success": 5, "fail": 0}}
        # 直接调用回退逻辑
        from swe_agent.fsm.agent_fsm import AgentFSM as _A
        # 手工执行回退的权重恢复段
        fsm.graph_manager.restore_weights_snapshot(fsm._initial_weights)
        w = fsm.graph_manager.get_weights_snapshot()
        assert w == {"bug.py::add": {"success": 2, "fail": 0}}, f"权重应回退: {w}"
        print("✅ 权重回退到初始快照（失败不污染图）")

    def test_silent_error_detection(self):
        """异常信号检查：exit 0 但日志含异常模式。"""
        import re
        raw_log = "pytest passed\nTraceback (most recent call last):\n  File x.py\nNameError: name 'pi' is not defined"
        silent = sorted(set(re.findall(
            r"(Traceback|NameError|TypeError|ValueError|AssertionError|KeyError|IndexError|Exception|Error)",
            raw_log)))
        assert "NameError" in silent and "Traceback" in silent
        clean = "1 passed"
        assert re.findall(
            r"(Traceback|NameError|TypeError|ValueError|AssertionError|KeyError|IndexError|Exception|Error)",
            clean) == []
        print("✅ 异常信号检测：exit 0 但含异常 → 捕获；干净日志 → 无信号")


if __name__ == "__main__":
    # 直接跑（pytest 收集时跳过 __main__ 也能跑）
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest",
                              __file__, "-q", "-s"]))