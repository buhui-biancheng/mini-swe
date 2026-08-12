"""AgentFSM 单元测试。"""

import os
import tempfile
import shutil
import pytest
from swe_agent.fsm.agent_fsm import AgentFSM, Watchdog, Checkpoint


@pytest.fixture
def sample_bug_project():
    """创建一个临时测试项目（含 bug）。"""
    tmpdir = tempfile.mkdtemp()
    project_dir = os.path.join(tmpdir, "bug_project")
    os.makedirs(project_dir)

    # 创建有 bug 的文件
    bug_code = '''\
def add(a, b):
    """返回两个数的和。"""
    return a - b  # Bug: 应该是 a + b
'''
    with open(os.path.join(project_dir, "bug.py"), "w") as f:
        f.write(bug_code)

    # 创建测试文件
    test_code = '''\
from bug import add

def test_add():
    assert add(1, 2) == 3
    assert add(-1, 1) == 0
'''
    with open(os.path.join(project_dir, "test_bug.py"), "w") as f:
        f.write(test_code)

    yield project_dir

    shutil.rmtree(tmpdir)


class TestWatchdog:
    """Watchdog 测试类（基于 DecisionEngine）。"""

    def test_record_tool_normal(self):
        """测试正常工具记录。"""
        w = Watchdog()
        # 相同工具+相同参数连续 3 次才触发
        assert w.record_tool("search_function", {"name": "add"}) is False
        assert w.record_tool("search_function", {"name": "add"}) is False
        assert w.record_tool("search_function", {"name": "add"}) is True  # 连续 3 次相同

    def test_record_tool_different_args(self):
        """测试不同参数不触发。"""
        w = Watchdog()
        assert w.record_tool("search_function", {"name": "add"}) is False
        assert w.record_tool("search_function", {"name": "mul"}) is False
        assert w.record_tool("search_function", {"name": "div"}) is False
        assert w.record_tool("search_function", {"name": "sub"}) is False
        assert w.record_tool("search_function", {"name": "pow"}) is False

    def test_record_state_normal(self):
        """测试正常状态记录。"""
        w = Watchdog()
        # 连续 5 次进入同一状态才触发
        for _ in range(4):
            assert w.record_state("locate") is False
        assert w.record_state("locate") is True

    def test_record_state_different(self):
        """测试不同状态不触发。"""
        w = Watchdog()
        for s in ["locate", "patch", "test", "locate", "patch"]:
            assert w.record_state(s) is False

    def test_record_edit(self):
        """测试编辑记录。"""
        w = Watchdog()
        w.record_edit("/tmp/test.py", True)
        w.record_edit("/tmp/test.py", True)
        assert w.engine.successful_edits == 2
        assert w.engine.edit_count == 2

    def test_check_stuck(self):
        """测试卡住检测。"""
        w = Watchdog()
        # 连续 6 轮没有编辑
        for _ in range(6):
            w.record_edit("/tmp/test.py", False)
        stuck, reason = w.check_stuck()
        assert stuck is True
        assert reason == "no_edit_rounds"

    def test_check_not_stuck(self):
        """测试正常情况不触发。"""
        w = Watchdog()
        w.record_edit("/tmp/test.py", True)
        stuck, _ = w.check_stuck()
        assert stuck is False

    def test_reset_all(self):
        """测试重置所有计数。"""
        w = Watchdog()
        w.record_state("locate")
        w.record_tool("search_function", {"name": "add"})
        w.reset_all()
        stuck, _ = w.check_stuck()
        assert stuck is False


class TestCheckpoint:
    """Checkpoint 测试类。"""

    def test_save_and_restore(self, sample_bug_project):
        """测试保存和恢复快照。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        cp = Checkpoint()

        # 保存快照
        cp.save(bug_file)

        # 修改文件
        with open(bug_file, "w") as f:
            f.write("def add(a, b):\n    return a * b\n")

        # 恢复快照
        assert cp.restore(bug_file) is True

        # 验证恢复
        with open(bug_file, "r") as f:
            content = f.read()
        assert "return a - b" in content

    def test_restore_nonexistent(self):
        """测试恢复不存在的快照。"""
        cp = Checkpoint()
        assert cp.restore("/nonexistent/file.py") is False

    def test_clear(self, sample_bug_project):
        """测试清除所有快照。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        cp = Checkpoint()
        cp.save(bug_file)
        assert len(cp.snapshots) == 1

        cp.clear()
        assert len(cp.snapshots) == 0


class TestAgentFSM:
    """AgentFSM 测试类。"""

    def test_init_state(self, sample_bug_project):
        """测试 FSM 初始化状态。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        fsm = AgentFSM(
            bug_file=bug_file,
            test_command="pytest test_bug.py -v",
            max_retries=2,
        )

        assert fsm.state == "init"
        assert fsm.bug_file == bug_file
        assert fsm.max_retries == 2

    def test_transitions_defined(self, sample_bug_project):
        """测试状态转换已定义。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        fsm = AgentFSM(
            bug_file=bug_file,
            test_command="pytest test_bug.py -v",
        )

        # 检查所有转换都已定义
        assert hasattr(fsm, "start")
        assert hasattr(fsm, "locate_done")
        assert hasattr(fsm, "patch_done")
        assert hasattr(fsm, "test_pass")
        assert hasattr(fsm, "test_fail")

    def test_graph_context_compact_format(self, sample_bug_project):
        """图上下文（2026-08-13 定稿）：L1 邻域注入保留；L2 细准按需（view_file 清单）。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        base_dir = os.path.dirname(bug_file)
        # L1（只有细准）：邻域注入保留
        fsm1 = AgentFSM(bug_file=bug_file, test_command="pytest test_bug.py -v",
                        mode="dp", graph_level=1)
        text1 = fsm1._graph_context_text()
        assert "NODE: " in text1 and "EDGE: " in text1, "L1 模式邻域注入保留"
        # L2（完整）：细准不注入（按需），粗准 L-1/L0 保留
        fsm2 = AgentFSM(bug_file=bug_file, test_command="pytest test_bug.py -v",
                        mode="dp", graph_level=2)
        text2 = fsm2._graph_context_text()
        assert "NODE: " not in text2 and "EDGE: " not in text2, "L2 细准不注入"
        assert "节点数" in text2 or "文件级" in text2, "L2 粗准保留"
        # 按需：view_file 清单只在 L2（fsm.registry 带 graph_index）
        listing2 = fsm2.registry._file_symbol_listing(os.path.join(base_dir, "bug.py"))
        listing1 = fsm1.registry._file_symbol_listing(os.path.join(base_dir, "bug.py"))
        assert listing2, "L2 按需清单有内容"
        assert not listing1, "L1 无按需清单（细准=注入）"

    def test_skeleton_generated(self, sample_bug_project):
        """测试骨架已生成。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        fsm = AgentFSM(
            bug_file=bug_file,
            test_command="pytest test_bug.py -v",
        )

        assert "add" in fsm.skeleton_text

    def test_watchdog_initialized(self, sample_bug_project):
        """测试 Watchdog 已初始化。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        fsm = AgentFSM(
            bug_file=bug_file,
            test_command="pytest test_bug.py -v",
        )

        assert fsm.watchdog is not None
        assert fsm.watchdog.engine is not None

    def test_checkpoint_initialized(self, sample_bug_project):
        """测试 Checkpoint 已初始化。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        fsm = AgentFSM(
            bug_file=bug_file,
            test_command="pytest test_bug.py -v",
        )

        assert fsm.checkpoint is not None

    def test_state_is_string(self, sample_bug_project):
        """测试状态是字符串类型。"""
        bug_file = os.path.join(sample_bug_project, "bug.py")
        fsm = AgentFSM(
            bug_file=bug_file,
            test_command="pytest test_bug.py -v",
        )

        assert isinstance(fsm.state, str)
        # Phase 2 新增 check / rollback 状态
        assert fsm.state in ["init", "locate", "patch", "check", "test", "rollback", "success", "fail"]
