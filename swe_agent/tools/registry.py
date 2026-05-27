import json
from typing import Any

from swe_agent.tools.schemas import (
    SearchFunctionArgs,
    ExpandFunctionArgs,
    EditFunctionArgs,
    RunTestArgs,
)
from swe_agent.ast_view.function_map import get_function_line_map, get_function_source
from swe_agent.sandbox.docker_runner import run_in_docker


class ToolRegistry:
    """工具注册与调度中心。"""

    def __init__(self, skeleton_text: str = "", code_dir: str = ".", python_version: str = "3.11", packages: list[str] | None = None):
        self.skeleton_text = skeleton_text
        self.code_dir = code_dir
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

        try:
            result = self._tools[tool_name](**arguments)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _search_function(self, name: str) -> dict:
        lines = self.skeleton_text.split("\n")
        matches = [line for line in lines if name.lower() in line.lower()]
        return {"matches": matches if matches else ["未找到匹配的函数"]}

    def _expand_function(self, file_path: str, func_name: str) -> dict:
        source = get_function_source(file_path, func_name)
        if source is None:
            return {"error": f"未找到函数 {func_name} in {file_path}"}
        return {"file": file_path, "function": func_name, "source": source}

    def _edit_function(self, file_path: str, start_line: int, end_line: int, new_code: str) -> dict:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if start_line < 1 or end_line > len(lines) or start_line > end_line:
            return {"error": f"行号范围无效: {start_line}-{end_line}，文件共 {len(lines)} 行"}

        new_lines = new_code if new_code.endswith("\n") else new_code + "\n"
        lines[start_line - 1 : end_line] = [new_lines]

        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        return {"success": True, "file": file_path, "lines_edited": f"{start_line}-{end_line}"}

    def _run_test(self, command: str) -> dict:
        result = run_in_docker(
            self.code_dir,
            command,
            python_version=self.python_version,
            packages=self.packages,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
