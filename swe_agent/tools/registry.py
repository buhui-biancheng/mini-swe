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
                 sandbox: bool = False, graph_level: int = 2,
                 block_network: bool = False):
        self.skeleton_text = skeleton_text
        self.code_dir = os.path.abspath(code_dir)
        self.python_version = python_version
        self.packages = packages or ["pytest"]
        self.graph_index = graph_index  # GraphIndex（迁移后优先使用）
        # 图按需读取（2026-08-13 用户定稿）：图不全量注入，view_file 时
        # 附带本文件符号清单（细准按需）；graph_level>=1 才附带（消融干净）
        self.graph_level = graph_level
        self.graph_manager = graph_manager  # Phase 5 JIT
        self.sandbox = sandbox  # Phase 6 补全用
        self.fence = fence              # PermissionFence（Phase 2 权限围栏）
        # 2026-08-13 官方模式网络防火墙：run_command 是宿主机 shell，
        # 不拦会成作弊通道（curl 下载上游题解）。block_network=True 时禁网络命令。
        self.block_network = block_network
        self._tools: dict[str, callable] = {
            "search_function": self._search_function,
            "view_file": self._view_file,
            "edit_function": self._edit_function,
            "report_graph_update": self._report_graph_update,  # Phase 5 JIT
            "run_test": self._run_test,
            "run_command": self._run_command,
            "set_plan": self._set_plan,  # 2026-08-16 计划清单
        }
        self.plan: list[dict] = []  # 2026-08-16 agent 声明的修复计划 [{task, done}]

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

    def _file_symbol_listing(self, file_path: str) -> str:
        """本文件函数/类清单（图索引按需细准，2026-08-13）。"""
        if self.graph_index is None or self.graph_level < 2:
            # 2026-08-13 修正：按需清单只给 L2（L1 的细准=注入的邻域，消融干净）
            return ""
        try:
            nodes = [n for n in self.graph_index.graph.nodes.values()
                     if os.path.basename(n.file) == os.path.basename(file_path)
                     and n.node_type.value in ("function", "class")]
        except Exception:
            return ""
        if not nodes:
            return ""
        nodes.sort(key=lambda x: x.lineno)
        lines = [f"# {file_path} 内符号（图索引）："]
        for n in nodes:
            lines.append(f"  {n.node_type.value}: {n.name} (L{n.lineno}-{n.end_lineno})")
        return "\n".join(lines)

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

    def _set_plan(self, plan: list) -> dict:
        """声明/更新修复计划（2026-08-16 治本：多目标全覆盖）。

        存储 {task, done} 列表；FSM 在交卷前检查未完成项。
        返回当前计划 + 待办 + 完成数，让 agent 能复查。
        """
        self.plan = [{"task": p.get("task", ""), "done": bool(p.get("done", False))} for p in plan]
        pending = [t["task"] for t in self.plan if not t["done"]]
        return {
            "plan": self.plan,
            "pending": pending,
            "pending_count": len(pending),
            "done_count": sum(1 for t in self.plan if t["done"]),
        }

    def _edit_function(self, file_path: str, old_string: str = None, new_string: str = None,
                       start_line: int = None, end_line: int = None, new_code: str = None,
                       insert_after: int = None) -> dict:
        # 2026-08-13 围栏：禁止修改测试文件（官方模式防自证陷阱——改测试获假信心）
        _rp = os.path.basename(file_path)
        _d = os.path.basename(os.path.dirname(file_path))
        if _d in ("tests", "test", "testing") or _rp.startswith("test_"):
            return {"error": "不允许修改测试文件（tests/ 目录只读——你的修改只能针对业务代码）",
                    "stdout": "", "stderr": "测试文件禁止修改", "exit_code": -1}
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
            for root, dirs, files in os.walk(self.code_dir):
                if os.path.basename(file_path) in files:
                    abs_path = os.path.join(root, os.path.basename(file_path))
                    break
        if not os.path.exists(abs_path):
            return {"error": f"文件不存在: {file_path}"}

        with open(abs_path, "r", encoding="utf-8") as f:
            text = f.read()
        lines = text.splitlines(keepends=True)

        # 模式C：insert_after → 在该行之后插入 new_code
        if insert_after is not None:
            if new_code is None:
                return {"error": "模式C 需要 insert_after（行号）+ new_code（要插入的代码）"}
            if insert_after < 0 or insert_after > len(lines):
                return {"error": f"insert_after 超出范围（文件共 {len(lines)} 行）"}
            insert_text = new_code if new_code.endswith("\n") else new_code + "\n"
            lines.insert(insert_after, insert_text)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write("".join(lines))
            result = {"success": True, "file": file_path, "lines_edited": f"插入@{insert_after+1}",
                      "start_line": insert_after + 1, "end_line": insert_after + 1, "mode": "insert",
                      "excerpt": "".join(lines[max(0, insert_after-1):insert_after+3]).rstrip()[:400]}
            if fence_warnings:
                result["warning"] = "；".join(fence_warnings)
                result["penalty"] = fence_penalty
            return result

        # 模式A：old_string → new_string 精确替换（唯一性检查 + 未找到恢复提示）
        if old_string is not None:
            count = text.count(old_string)
            if count == 0:
                return self._edit_not_found_hint(file_path, lines, old_string)
            if count > 1:
                # 报告出现位置的行号（str_replace_editor 风格）——让模型知道在哪加上下文
                occ_lines = []
                cursor = 0
                for _ in range(count):
                    idx = text.index(old_string, cursor)
                    occ_lines.append(text[:idx].count("\n") + 1)
                    cursor = idx + len(old_string)
                return {"error": f"old_string 在文件中出现 {count} 次（第 {occ_lines} 行），不唯一。"
                                "请带上更多上下文（前/后几行）让 old_string 精确匹配唯一位置。"}
            idx = text.index(old_string)
            prefix = text[:idx]
            act_start = prefix.count("\n") + 1
            act_end = act_start + old_string.count("\n")
            text = text[:idx] + (new_string or "") + text[idx + len(old_string):]
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(text)
            new_lines = text.splitlines(keepends=True)
            lo = max(0, act_start - 2); hi = min(len(new_lines), act_end + 2)
            result = {"success": True, "file": file_path, "lines_edited": f"{act_start}-{act_end}",
                      "start_line": act_start, "end_line": act_end, "mode": "old_string",
                      "excerpt": "".join(new_lines[lo:hi]).rstrip()[:400]}
            if fence_warnings:
                result["warning"] = "；".join(fence_warnings)
                result["penalty"] = fence_penalty
            return result

        # 模式B：start_line/end_line 行范围替换
        if start_line is None or end_line is None or new_code is None:
            return {"error": "请提供编辑参数（不能全空）：模式A old_string+new_string；"
                            "或模式B start_line+end_line+new_code；或模式C insert_after+new_code。"}
        if start_line < 1 or end_line > len(lines) or start_line > end_line:
            return {"error": f"行号范围无效: {start_line}-{end_line}，文件共 {len(lines)} 行"}

        new_lines = new_code if new_code.endswith("\n") else new_code + "\n"
        lines[start_line - 1 : end_line] = [new_lines]
        with open(abs_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        result = {"success": True, "file": file_path, "lines_edited": f"{start_line}-{end_line}",
                  "start_line": start_line, "end_line": end_line, "mode": "line_range",
                  "excerpt": "".join(lines[max(0, start_line-2):min(len(lines), end_line+2)]).rstrip()[:400]}
        if fence_warnings:
            result["warning"] = "；".join(fence_warnings)
            result["penalty"] = fence_penalty
        return result

    def _edit_not_found_hint(self, file_path: str, lines: list, old_string: str) -> dict:
        """old_string 未找到时的恢复提示：引用原文 + 模糊匹配附近行，让模型复制精确文本。"""
        import re as _re
        norm = _re.sub(r"\s+", " ", old_string).strip()
        key = norm[:25]
        hits = [i + 1 for i, l in enumerate(lines) if key and key.lower() in l.lower()]
        snippet = ""
        if hits:
            first = hits[0]
            lo = max(0, first - 3); hi = min(len(lines), first + 3)
            snippet = "\n".join(f"{i+1}: {lines[i].rstrip()}" for i in range(lo, hi))
        msg = (f"未执行替换：old_str `{old_string[:80]}` 未原样出现在 {file_path} 中。"
               + f"（模糊匹配尝试: {norm[:50]}...）"
               + (f"\n疑似附近（第 {hits[:5]} 行）：\n{snippet}" if snippet else "")
               + "\n请复制文件中的精确文本（含缩进与空行）。")
        return {"error": msg}

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
            reuse=True,  # 2026-08-13 容器复用
        )
        return {
            "stdout": _truncate_output(result.stdout),
            "stderr": _truncate_output(result.stderr, 1500),
            "exit_code": result.exit_code,
        }

    def _run_command(self, command: str) -> dict:
        """运行终端命令（2026-08-14 安全改造：非 git 命令进容器，git 留宿主）。

        - 非 git（grep/python/ls/脚本）：进容器执行——network=False 天然断网（堵死
          curl/git-show 类作弊通道）+ 正确依赖环境；容器隔离，失败不影响宿主。
        - git：留宿主——worktree 的 .git 是文件，对象库在主仓库，容器内 git 不可用。
          git 走宿主时仍过网络/历史防火墙。
        """
        import subprocess

        # git 检测：任一 && 段以 git 开头
        segments = [s.strip() for s in command.split("&&") if s.strip()]
        is_git = any(seg == "git" or seg.startswith("git ") for seg in segments)

        if not is_git:
            # 容器执行（network=False 默认断网，reuse=True 复用 run_test 容器）
            try:
                from swe_agent.sandbox.docker_runner import run_in_docker
                container_cmd = command
                if self.code_dir in container_cmd:
                    container_cmd = container_cmd.replace(self.code_dir, "/workspace")
                r = run_in_docker(self.code_dir, container_cmd, timeout=60,
                                  python_version=self.python_version,
                                  packages=self.packages, reuse=True)
                return {"stdout": _truncate_output(r.stdout, 8000),
                        "stderr": _truncate_output(r.stderr, 1500),
                        "exit_code": r.exit_code}
            except Exception as e:
                return {"error": f"容器命令执行失败: {e}"}

        # ===== git → 宿主 =====
        # Phase 6 沙盒模式：命令在容器内执行（只读挂载+tmpfs），Agent 碰不到宿主机
        if self.sandbox:
            from swe_agent.sandbox.docker_runner import run_in_docker
            r = run_in_docker(self.code_dir, command, timeout=60)
            return {"stdout": _truncate_output(r.stdout, 8000), "stderr": _truncate_output(r.stderr, 1500),
                    "exit_code": r.exit_code}

        # 危险命令
        dangerous = ["rm -rf", "sudo", "chmod 777", "mkfs", "dd if="]
        for d in dangerous:
            if d in command.lower():
                return {"error": f"危险命令被禁止: {command}"}

        # 官方模式网络防火墙（git 走宿主仍有网，需拦网络/历史命令）
        if self.block_network:
            _net = ["curl", "wget", "pip install", "pip3 install",
                    "pip download", "pip3 download", "git clone",
                    "git fetch", "git pull", "git remote", "http://", "https://",
                    "api.github", "raw.githubusercontent", "patch-diff",
                    "urllib.request", "requests.get",
                    # 2026-08-13 堵 git 历史/tag 泄漏：本地仓库有全部 tag，
                    # git show <tag>:file 能直接读出未来版本实现（实测 agent 用过）
                    "git show", "git log", "git tag", "git branch",
                    "git rev-list", "git ls-remote", "git describe", "git blame",
                    "HEAD~", "^HEAD"]
            if any(k in command.lower() for k in _net):
                return {"error": "网络命令被禁止（官方模式离线）——请只使用本地仓库代码推理，"
                                "不要尝试联网获取外部代码",
                        "stdout": "", "stderr": "network blocked", "exit_code": -1}

        # 执行命令
        try:
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
