import json
import os
from typing import Any

from swe_agent.tools.schemas import (
    SearchFunctionArgs,
    ViewFileArgs,
    EditFunctionArgs,
    RunTestArgs,
    validate_tool_args,
)
from swe_agent.ast_view.function_map import get_function_line_map, get_function_source
from swe_agent.sandbox.docker_runner import run_in_docker


# 工具输出截断（2026-08-13 对齐行业标准 SWE-agent max_observation_length/Claude Code）：
# 工具结果全部进对话历史且每轮重发——超长输出（pytest 全量/大文件）是
# token 膨胀主因。截断保留尾部（pytest 失败摘要通常在末尾）+ clipped 标记。
OUTPUT_TRUNCATE_CHARS = 4000


def _truncate_output(text: str, limit: int = OUTPUT_TRUNCATE_CHARS) -> str:
    """智能截断工具输出（2026-08-13 边界处理）：
    优先保留【关键段】——pytest FAILURES/ERRORS 段（失败原因=核心信号）
    + 尾部（统计行）；其次才头尾。失败信息永不丢。
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    import re as _re
    # 关键段：pytest 失败/错误详情（FAILURES 段/ERRORS 段/traceback）
    key_parts = []
    for pat in (r"={5,} FAILURES ={5,}.*?(?=\n={5,}|\Z)",
                r"={5,} ERRORS ={5,}.*?(?=\n={5,}|\Z)",
                r"_{5,} .*? _{5,}.*?(?=\n_{5,}|\Z)"):
        for m in _re.finditer(pat, text, _re.S):
            seg = m.group(0)
            if len(seg) > 200:  # 有效失败段
                key_parts.append(seg)
                break
    # 尾部（统计行：x passed, y failed）
    tail = text[-800:]
    if key_parts:
        # 关键段 + 尾部，总长控制
        joined = "\n\n".join(key_parts[:2]) + "\n\n" + tail
        if len(joined) <= limit:
            return joined
        return joined[:limit // 2] + f"\n...[输出截断]...\n" + joined[-limit // 2:]
    # 无失败段（成功输出：点点点）——保留头部+尾部
    head = limit // 4
    return (text[:head] + f"\n...[输出截断，省略 {len(text) - limit} 字符]...\n"
            + text[-limit + head:])


class ToolRegistry:
    """工具注册与调度中心（Phase 2 简化：5 工具）。"""

    def __init__(self, skeleton_text: str = "", code_dir: str = ".",
                 python_version: str = "3.11", packages: list[str] | None = None,
                 graph_index=None, fence=None, graph_manager=None,
                 sandbox: bool = False):
        self.skeleton_text = skeleton_text
        self.code_dir = os.path.abspath(code_dir)
        self.python_version = python_version
        self.packages = packages or ["pytest"]
        self.graph_index = graph_index  # GraphIndex（迁移后优先使用）
        self.graph_manager = graph_manager  # Phase 5 JIT
        self.sandbox = sandbox  # Phase 6 补全用
        self.fence = fence              # PermissionFence（Phase 2 权限围栏）
        self._tools: dict[str, callable] = {
            "search_function": self._search_function,
            "view_file": self._view_file,
            "edit_function": self._edit_function,
            "report_graph_update": self._report_graph_update,  # Phase 5 JIT
            "run_test": self._run_test,
            "run_command": self._run_command,
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
        # 迁移后优先查询图索引节点
        if self.graph_index is not None:
            results = self.graph_index.search_function(name)
            if results:
                matches = [
                    f"{r['node']} (lines {r['lines']}, in_degree={r['in_degree']})"
                    for r in results[:20]
                ]
                return {"matches": matches}
            return {"matches": ["未找到匹配的函数"]}

        lines = self.skeleton_text.split("\n")
        matches = [line for line in lines if name.lower() in line.lower()]
        return {"matches": matches if matches else ["未找到匹配的函数"]}

    def _resolve_path(self, file_path: str) -> str:
        """尝试多种方式解析文件路径。"""
        abs_path = os.path.abspath(file_path)
        if os.path.exists(abs_path):
            return abs_path
        abs_path = os.path.join(self.code_dir, file_path)
        if os.path.exists(abs_path):
            return abs_path
        # 在 code_dir 下按文件名查找
        for root, dirs, files in os.walk(self.code_dir):
            if os.path.basename(file_path) in files:
                return os.path.join(root, os.path.basename(file_path))
        return ""

    def _view_file(self, file_path: str, function: str = None, line: int = None,
                   context: int = 0, start_line: int = None, end_line: int = None) -> dict:
        """查看文件（三模式，Phase 2 模块 D3）。

        模式 1（function）：读整个函数，未精确匹配返回模糊候选
        模式 2（line + context）：报错行周围
        模式 3（start_line + end_line）：精确行范围（含读日志）
        """
        # 模式 1：语义读（吸收原 expand_function）
        if function:
            return self._view_function(file_path, function)

        # 模式 2：定位读（line 给出）
        if line is not None:
            if start_line is None:
                start_line = max(1, line - context) if context > 0 else line
            if end_line is None:
                end_line = line + context if context > 0 else line

        # 模式 3：范围读（context 也可附带展开 start/end）
        if context > 0:
            if start_line is not None and start_line > 0:
                start_line = max(1, start_line - context)
            if end_line is not None and end_line > 0:
                end_line = end_line + context

        if start_line is None:
            start_line = 1
        if end_line is None:
            end_line = 0  # 0 表示到文件末尾

        return self._read_lines(file_path, start_line, end_line)

    def _view_function(self, file_path: str, func_name: str) -> dict:
        """模式 1：读整个函数（查图节点行号范围）。"""
        if self.graph_index is not None:
            node = self.graph_index.find_node(file_path, func_name)
            if node is not None:
                return self._read_lines(node.file, node.lineno, node.end_lineno, label=func_name)
            # 未精确匹配 → 返回模糊候选（兜底，不报死）
            matches = self.graph_index.search_nodes(func_name, limit=10)
            return {"function": func_name, "candidates": [m.node_id for m in matches]}

        # 无图时退化为旧逻辑（统一返回 content 键）
        abs_path = self._resolve_path(file_path)
        if not abs_path:
            return {"error": f"文件不存在: {file_path}"}
        source = get_function_source(abs_path, func_name)
        if source is None:
            return {"error": f"未找到函数 {func_name} in {file_path}"}
        return {
            "file": file_path,
            "function": func_name,
            "content": source,
            "start_line": 1,
            "end_line": 0,
        }

    def _read_lines(self, file_path: str, start_line: int, end_line: int,
                    label: str = "") -> dict:
        """按行范围读取文件（带行号）。"""
        abs_path = self._resolve_path(file_path)
        if not abs_path:
            return {"error": f"文件不存在: {file_path}"}

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            return {"error": f"读取文件失败: {e}"}

        total_lines = len(lines)
        start_line = max(1, start_line)
        end_line = min(total_lines, end_line) if end_line > 0 else total_lines

        if start_line > total_lines:
            return {"error": f"起始行号 {start_line} 超出文件总行数 {total_lines}"}
        if start_line > end_line:
            return {"error": f"起始行号 {start_line} 大于结束行号 {end_line}"}

        content_lines = []
        for i in range(start_line - 1, end_line):
            content_lines.append(f"{i + 1:4d} | {lines[i].rstrip()}")

        result = {
            "file": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "content": "\n".join(content_lines),
        }
        if label:
            result["function"] = label
        return result

    def _report_graph_update(self, node_id: str, target: str,
                             edge_type: str, evidence: str) -> dict:
        """JIT 图谱补全：提交补全建议 → 系统验证 → 写入。

        返回 dict（execute 统一 json.dumps，避免双重序列化）。
        """
        if self.graph_index is None:
            return {"error": "JIT 补全不可用：无图索引"}
        if self.graph_manager is None:
            return {"error": "JIT 补全不可用：无 GraphManager"}
        return self.graph_manager.apply_jit_update(node_id, target, edge_type, evidence)

    def _edit_function(self, file_path: str, start_line: int, end_line: int, new_code: str) -> dict:
        # 围栏软约束：警告 + 代价惩罚，不拦截（bug 所在文件必须能修）
        fence_warnings = []
        fence_penalty = 1.0
        if self.fence is not None:
            fence_check = self.fence.check_edit(file_path)
            fence_warnings = fence_check.warnings
            fence_penalty = fence_check.penalty

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

        result = {"success": True, "file": file_path, "lines_edited": f"{start_line}-{end_line}"}
        if fence_warnings:
            result["warning"] = "；".join(fence_warnings)
            result["penalty"] = fence_penalty
        return result

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
            "stdout": _truncate_output(result.stdout),
            "stderr": _truncate_output(result.stderr, 1500),
            "exit_code": result.exit_code,
        }

    def _run_command(self, command: str) -> dict:
        """在宿主机上运行终端命令。"""
        import subprocess

        try:
            # 安全检查：禁止危险命令
            dangerous = ["rm -rf", "sudo", "chmod 777", "mkfs", "dd if="]
            # Phase 6 沙盒模式：命令在容器内执行（只读挂载+tmpfs），Agent 碰不到宿主机
            if self.sandbox:
                from swe_agent.sandbox.docker_runner import run_in_docker
                r = run_in_docker(self.code_dir, command, timeout=60)
                return {"stdout": _truncate_output(r.stdout), "stderr": _truncate_output(r.stderr, 1500),
                        "exit_code": r.exit_code}
            for d in dangerous:
                if d in command.lower():
                    return {"error": f"危险命令被禁止: {command}"}

            # 执行命令
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.code_dir,
            )

            return {
                "stdout": result.stdout[:2000] if result.stdout else "",
                "stderr": result.stderr[:500] if result.stderr else "",
                "exit_code": result.returncode,
            }

        except subprocess.TimeoutExpired:
            return {"error": f"命令执行超时: {command}"}
        except Exception as e:
            return {"error": f"命令执行失败: {e}"}
