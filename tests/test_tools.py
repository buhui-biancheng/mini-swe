import os
import json
import tempfile
import pytest
from swe_agent.tools.schemas import (
    SearchFunctionArgs,
    ViewFileArgs,
    EditFunctionArgs,
    RunTestArgs,
    TOOLS,
)
from swe_agent.tools.registry import ToolRegistry


SAMPLE_CODE = '''\
def hello():
    return "world"

def add(a, b):
    return a + b

class Calculator:
    def multiply(self, x, y):
        return x * y
'''


@pytest.fixture
def sample_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(SAMPLE_CODE)
        f.flush()
        yield f.name
    os.unlink(f.name)


@pytest.fixture
def registry(sample_file):
    skeleton = f"{sample_file}: def hello (1-2), def add (4-5), class Calculator (7-9)"
    return ToolRegistry(skeleton_text=skeleton, code_dir=os.path.dirname(sample_file))


class TestSchemas:
    def test_search_function_args(self):
        args = SearchFunctionArgs(name="foo")
        assert args.name == "foo"

    def test_view_file_args(self):
        args = ViewFileArgs(file_path="test.py", start_line=3, end_line=5)
        assert args.file_path == "test.py"
        assert args.start_line == 3
        assert args.end_line == 5

    def test_view_file_args_function_mode(self):
        args = ViewFileArgs(file_path="test.py", function="hello")
        assert args.function == "hello"
        assert args.line is None

    def test_view_file_args_line_mode(self):
        args = ViewFileArgs(file_path="test.py", line=42, context=5)
        assert args.line == 42
        assert args.context == 5

    def test_view_file_args_with_context(self):
        args = ViewFileArgs(file_path="test.py", start_line=3, end_line=3, context=2)
        assert args.start_line == 3
        assert args.context == 2

    def test_edit_function_args(self):
        args = EditFunctionArgs(
            file_path="test.py", start_line=1, end_line=5, new_code="pass"
        )
        assert args.start_line == 1
        assert args.end_line == 5

    def test_run_test_args(self):
        args = RunTestArgs(command="pytest test.py")
        assert args.command == "pytest test.py"

    def test_tools_list_length(self):
        # Phase 2 简化：6 → 5 工具；Phase 5 JIT：+report_graph_update → 6
        # 2026-08-16：+set_plan → 7；2026-08-21：+write_file → 8
        # 2026-08-21 精简：-search_function -report_graph_update → 6
        #（读/写/改/终端×2/方案；registry 实现保留，schema 不暴露）
        assert len(TOOLS) == 7

    def test_tools_have_correct_types(self):
        for tool in TOOLS:
            assert tool["type"] == "function"
            assert "name" in tool["function"]
            assert "parameters" in tool["function"]


class TestToolRegistry:
    def test_search_function(self, registry):
        result = json.loads(registry.execute("search_function", {"name": "hello"}))
        assert "matches" in result
        assert any("hello" in m for m in result["matches"])

    def test_search_function_not_found(self, registry):
        result = json.loads(registry.execute("search_function", {"name": "nonexistent"}))
        assert "matches" in result
        assert "未找到" in result["matches"][0]

    def test_view_file_function_mode(self, registry, sample_file):
        """模式 1：function 读整个函数（吸收 expand_function）。"""
        result = json.loads(
            registry.execute("view_file", {"file_path": sample_file, "function": "hello"})
        )
        assert "content" in result
        assert "def hello" in result["content"]
        assert result["function"] == "hello"

    def test_view_file_function_mode_not_found(self, registry, sample_file):
        """模式 1 未精确匹配 → 返回模糊候选（兜底，不报死）。"""
        result = json.loads(
            registry.execute("view_file", {"file_path": sample_file, "function": "nope"})
        )
        assert "candidates" in result or "error" in result

    def test_view_file(self, registry, sample_file):
        result = json.loads(
            registry.execute("view_file", {"file_path": sample_file, "start_line": 1, "end_line": 3})
        )
        assert "content" in result
        assert "def hello" in result["content"]
        assert result["start_line"] == 1
        assert result["end_line"] == 3

    def test_view_file_with_context(self, registry, sample_file):
        result = json.loads(
            registry.execute("view_file", {"file_path": sample_file, "start_line": 2, "end_line": 2, "context": 1})
        )
        assert "content" in result
        # context=1 会展开到第 1-3 行
        assert result["start_line"] == 1
        assert result["end_line"] == 3

    def test_view_file_line_overflow(self, registry, sample_file):
        result = json.loads(
            registry.execute("view_file", {"file_path": sample_file, "start_line": 999})
        )
        assert "error" in result

    def test_view_file_not_found(self, registry):
        result = json.loads(
            registry.execute("view_file", {"file_path": "/nonexistent/path.py"})
        )
        assert "error" in result

    def test_edit_function(self, registry, sample_file):
        result = json.loads(
            registry.execute(
                "edit_function",
                {
                    "file_path": sample_file,
                    "start_line": 1,
                    "end_line": 2,
                    "new_code": "def hello():\n    return 'edited'",
                },
            )
        )
        assert result.get("success") is True

        with open(sample_file, "r") as f:
            content = f.read()
        assert "edited" in content

    def test_edit_function_invalid_lines(self, registry, sample_file):
        result = json.loads(
            registry.execute(
                "edit_function",
                {
                    "file_path": sample_file,
                    "start_line": 999,
                    "end_line": 1000,
                    "new_code": "pass",
                },
            )
        )
        assert "error" in result

    def test_unknown_tool(self, registry):
        result = json.loads(registry.execute("unknown_tool", {}))
        assert "error" in result

    def test_run_test_command(self, registry):
        result = json.loads(
            registry.execute("run_test", {"command": "echo hello"})
        )
        assert "stdout" in result
        assert "exit_code" in result


class TestWriteFile:
    """write_file 工具（2026-08-21）：新建 / 整文件覆写 / 围栏。"""

    def test_create_new_file(self, registry, tmp_path):
        target = os.path.join(tmp_path, "sub", "new_mod.py")
        result = json.loads(registry.execute(
            "write_file", {"file_path": target, "content": "x = 1\n"}))
        assert result.get("success") is True
        assert result["mode"] == "create"
        assert result["old_lines"] == 0
        assert result["new_lines"] == 1
        assert os.path.exists(target)
        with open(target) as f:
            assert f.read() == "x = 1\n"

    def test_overwrite_existing(self, registry, sample_file):
        result = json.loads(registry.execute(
            "write_file", {"file_path": sample_file, "content": "y = 2\n"}))
        assert result.get("success") is True
        assert result["mode"] == "overwrite"
        assert result["old_lines"] >= 5
        with open(sample_file) as f:
            assert f.read() == "y = 2\n"

    def test_relative_path_creates_under_code_dir(self, registry):
        result = json.loads(registry.execute(
            "write_file", {"file_path": "brand_new.py", "content": "z = 3\n"}))
        assert result.get("success") is True
        target = os.path.join(registry.code_dir, "brand_new.py")
        assert os.path.exists(target)
        os.unlink(target)  # 清理

    def test_reject_tests_dir(self, registry, tmp_path):
        t = tmp_path / "tests" / "test_x.py"
        t.parent.mkdir()
        t.write_text("")
        result = json.loads(registry.execute(
            "write_file", {"file_path": str(t), "content": "print(1)"}))
        assert "error" in result
        assert "测试文件" in result["error"]

    def test_reject_env_file(self, registry, tmp_path):
        result = json.loads(registry.execute(
            "write_file", {"file_path": str(tmp_path / ".env"), "content": "KEY=x"}))
        assert "error" in result
        assert "敏感路径" in result["error"]

    def test_content_too_large(self, registry, tmp_path):
        result = json.loads(registry.execute(
            "write_file", {"file_path": str(tmp_path / "big.py"), "content": "x" * 60000}))
        assert "error" in result
        assert "过大" in result["error"]


class TestCheckpointCreatedFile:
    """Checkpoint 新建文件回滚（2026-08-21）：write_file 新建的文件，回滚 = 删除。"""

    def test_rollback_deletes_created_file(self, tmp_path):
        from swe_agent.fsm.agent_fsm import Checkpoint
        cp = Checkpoint()
        target = tmp_path / "new.py"
        cp.save_initial(str(target))  # 文件尚不存在 → 记入 created
        target.write_text("x = 1\n")
        assert cp.restore(str(target)) is True
        assert not target.exists()

    def test_rollback_restores_existing_file(self, tmp_path):
        from swe_agent.fsm.agent_fsm import Checkpoint
        cp = Checkpoint()
        target = tmp_path / "f.py"
        target.write_text("old\n")
        cp.save_initial(str(target))
        target.write_text("new\n")
        assert cp.restore(str(target)) is True
        assert target.read_text() == "old\n"

    def test_clear_resets_created(self, tmp_path):
        from swe_agent.fsm.agent_fsm import Checkpoint
        cp = Checkpoint()
        cp.save_initial(str(tmp_path / "a.py"))
        cp.clear()
        assert not cp.created
