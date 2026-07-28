import os
import json
import tempfile
import pytest
from swe_agent.tools.schemas import (
    SearchFunctionArgs,
    ExpandFunctionArgs,
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

    def test_expand_function_args(self):
        args = ExpandFunctionArgs(file_path="test.py", func_name="foo")
        assert args.file_path == "test.py"
        assert args.func_name == "foo"

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
        assert len(TOOLS) == 5

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

    def test_expand_function(self, registry, sample_file):
        result = json.loads(
            registry.execute("expand_function", {"file_path": sample_file, "func_name": "hello"})
        )
        assert "source" in result
        assert "def hello" in result["source"]

    def test_expand_function_not_found(self, registry, sample_file):
        result = json.loads(
            registry.execute("expand_function", {"file_path": sample_file, "func_name": "nope"})
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
