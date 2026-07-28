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
    """Watchdog 测试类。"""

    def test_record_state_normal(self):
        """测试正常状态记录。"""
        w = Watchdog(max_same_state=3)
        assert w.record_state("locate") is False
        assert w.record_state("locate") is False
        assert w.record_state("locate") is True  # 第 3 次触发

    def test_record_tool_normal(self):
        """测试正常工具记录。"""
        w = Watchdog(max_same_tool=3)
        assert w.record_tool("search_function") is False
        assert w.record_tool("search_function") is False
        assert w.record_tool("search_function") is True  # 第 3 次触发

    def test_default_limits(self):
        """测试默认限制值。"""
        w = Watchdog()
        assert w.max_same_state == 10
        assert w.max_same_tool == 8

    def test_reset_state(self):
        """测试重置状态计数。"""
        w = Watchdog()
        w.record_state("locate")
        w.record_state("locate")
        w.reset_state("locate")
        assert w.record_state("locate") is False  # 重置后重新计数

    def test_reset_tool(self):
        """测试重置工具计数。"""
        w = Watchdog()
        w.record_tool("search_function")
        w.reset_tool("search_function")
        assert w.record_tool("search_function") is False  # 重置后重新计数

    def test_reset_all(self):
        """测试重置所有计数。"""
        w = Watchdog()
        w.record_state("locate")
        w.record_tool("search_function")
        w.reset_all()
        assert w.state_counts == {}
        assert w.tool_counts == {}


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
        assert hasattr(fsm, "locate_fail")
        assert hasattr(fsm, "patch_fail")
        assert hasattr(fsm, "max_retries")

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
        assert fsm.watchdog.max_same_state == 10
        assert fsm.watchdog.max_same_tool == 8

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
        assert fsm.state in ["init", "locate", "patch", "test", "success", "fail"]
