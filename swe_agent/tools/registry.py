import json
import os
from typing import Any

from swe_agent.tools.schemas import (
    SearchFunctionArgs,
    ExpandFunctionArgs,
    EditFunctionArgs,
    RunTestArgs,
    validate_tool_args,
)
from swe_agent.ast_view.function_map import get_function_line_map, get_function_source
from swe_agent.sandbox.docker_runner import run_in_docker


class ToolRegistry:
    """工具注册与调度中心。"""

    def __init__(self, skeleton_text: str = "", code_dir: str = ".", python_version: str = "3.11", packages: list[str] | None = None):
        self.skeleton_text = skeleton_text
        self.code_dir = os.path.abspath(code_dir)
        self.python_version = python_version
        self.packages = packages or ["pytest"]
        self._tools: dict[str, callable] = {
            "search_function": self._search_function,
            "expand_function": self._expand_function,
            "edit_function": self._edit_function,
            "run_test": self._run_test,
        }

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name not in self._tools:
            return json.dumps({"error": f"未知工具: {tool_name}"}, ensure_ascii=False)

        # Pydantic Schema 校验
        is_valid, error_msg, validated_args = validate_tool_args(tool_name, arguments)
        if not is_valid:
            return json.dumps({"error": error_msg}, ensure_ascii=False)

        try:
            result = self._tools[tool_name](**validated_args)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _to_container_path(self, host_path: str) -> str:
        """将宿主机路径转换为容器内路径（/workspace/...）。"""
        abs_path = os.path.abspath(host_path)
        if abs_path.startswith(self.code_dir):
            rel_path = os.path.relpath(abs_path, self.code_dir)
            return f"/workspace/{rel_path}"
        return abs_path

    def _search_function(self, name: str) -> dict:
        lines = self.skeleton_text.split("\n")
        matches = [line for line in lines if name.lower() in line.lower()]
        return {"matches": matches if matches else ["未找到匹配的函数"]}

    def _expand_function(self, file_path: str, func_name: str) -> dict:
        # 尝试多种路径
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            abs_path = os.path.join(self.code_dir, file_path)
        if not os.path.exists(abs_path):
            # 尝试在 code_dir 下查找
            for root, dirs, files in os.walk(self.code_dir):
                if os.path.basename(file_path) in files:
                    abs_path = os.path.join(root, os.path.basename(file_path))
                    break

        source = get_function_source(abs_path, func_name)
        if source is None:
            return {"error": f"未找到函数 {func_name} in {file_path}"}
        return {"file": file_path, "function": func_name, "source": source}

    def _edit_function(self, file_path: str, start_line: int, end_line: int, new_code: str) -> dict:
        # 尝试多种路径
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            abs_path = os.path.join(self.code_dir, file_path)
        if not os.path.exists(abs_path):
            # 尝试在 code_dir 下查找
            for root, dirs, files in os.walk(self.code_dir):
                if os.path.basename(file_path) in files:
                    abs_path = os.path.join(root, os.path.basename(file_path))
                    break

        with open(abs_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if start_line < 1 or end_line > len(lines) or start_line > end_line:
            return {"error": f"行号范围无效: {start_line}-{end_line}，文件共 {len(lines)} 行"}

        new_lines = new_code if new_code.endswith("\n") else new_code + "\n"
        lines[start_line - 1 : end_line] = [new_lines]

        with open(abs_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        return {"success": True, "file": file_path, "lines_edited": f"{start_line}-{end_line}"}

    def _run_test(self, command: str) -> dict:
        # 将命令中的宿主机绝对路径转换为容器内路径
        container_command = command

        # 1. 如果包含绝对路径，转换为容器路径
        if self.code_dir in command:
            container_command = command.replace(self.code_dir, "/workspace")

        # 2. 如果包含相对路径（如 examples/test.py），需要转换为容器内的相对路径
        # Docker 容器中工作目录是 /workspace，所以 "examples/test.py" 应该变成 "test.py"
        # 因为 code_dir 就是 examples 目录
        import re
        # 匹配 "examples/"、"tests/" 等目录前缀
        container_command = re.sub(r'(?:^|\s)(?:examples|tests|eval)/', ' ', container_command)
        # 清理多余的空格
        container_command = ' '.join(container_command.split())

        result = run_in_docker(
            self.code_dir,
            container_command,
            python_version=self.python_version,
            packages=self.packages,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
