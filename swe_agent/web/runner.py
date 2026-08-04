"""FSM 子进程运行器：解析 stdout 标记 → waku 格式事件流。

子进程方案与 TUI 一致（tui/app.py::_run_agent_fix）：spawn
`python -m swe_agent.main fix <bug> <test> --fsm`，逐行解析 stdout 标记，
翻译成 waku dashboard 认识的事件。事件经 on_event(kind, ev) 同步吐出；
服务器负责双写：JSONL trace（type 字段，供轮询亮灯）+ SSE（kind 字段，
供聊天 dock 流式渲染）。

图事件（graph_start/node_start/node_end/graph_end）驱动前端拓扑图高亮：
- graph_start 时点亮 INIT 上下文（bug 文件函数 + 1 跳邻居）
- 每个工具调用触达的文件 → 点亮对应节点（高亮跟随 AI 移动）
"""

import json
import os
import re
import subprocess
import time

STATE_LABELS = {
    "init": "初始化", "locate": "定位", "patch": "补丁", "check": "检查",
    "test": "测试", "rollback": "回滚", "success": "成功", "fail": "失败",
}

_TOOL_CALL_RE = re.compile(r"\[TOOL\] 调用 (\w+)\((.*)\)$")
_TEXT_TAGS = ("[PATCH]", "[CHECK]", "[ROLLBACK]", "[MODE]", "[BUDGET]",
              "[SYNTAX]", "[LOG]", "[WATCHDOG]", "[ERROR]")

WORKFLOW = "code-graph"


class FixRunner:
    """一次修复任务的运行器（同步阻塞，由服务器在请求线程内调用）。"""

    def __init__(self, project_root: str, bug_file: str, test_command: str,
                 on_event, graph=None):
        self.project_root = project_root
        self.bug_file = bug_file
        self.test_command = test_command or "pytest"
        self.on_event = on_event
        self.graph = graph
        self.code_dir = os.path.dirname(
            os.path.abspath(os.path.join(project_root, bug_file)))
        self._pending_tool = None          # (name, args)，等 成功/错误 收尾
        self._hot: set[str] = set()        # 当前高亮节点
        self._iterations = 0
        self._final_reply = ""

    # ---------- 事件辅助 ----------

    def _emit_node_focus(self, nodes: list[str]) -> None:
        """高亮跟随 AI：先熄灭上一组，再点亮新一组。"""
        for n in self._hot:
            self.on_event("node_end", {"workflow": WORKFLOW, "node": n})
        self._hot = set(nodes)
        for n in nodes:
            self.on_event("node_start", {"workflow": WORKFLOW, "node": n})

    def _finalize_tool(self, status: str) -> None:
        if not self._pending_tool:
            return
        name, args = self._pending_tool
        self._pending_tool = None
        self.on_event("tool", {"tool": name, "args": args, "output": status})

    # ---------- 标记解析 ----------

    def _handle_line(self, line: str) -> None:
        if "[STATE] " in line:
            m = re.search(r"\[STATE\] (\w+)(?: \(第 (\d+) 次尝试\))?", line)
            if m:
                state = m.group(1)
                attempt = int(m.group(2)) if m.group(2) else 0
                self.on_event("text", {"delta": f"\n▶ 状态 → {STATE_LABELS.get(state, state)}\n"})
                self.on_event("state", {"state": state, "attempt": attempt})
            return

        m = _TOOL_CALL_RE.search(line)
        if m and "[TOOL] 调用 " in line:
            name = m.group(1)
            args_raw = m.group(2)
            try:
                args = json.loads(args_raw) if args_raw.strip() else {}
            except Exception:
                args = {"raw": args_raw[:200]}
            self._pending_tool = (name, args)
            self._iterations += 1
            if self.graph is not None:
                nodes = self.graph.nodes_for_tool(self.code_dir, name, args)
                if nodes:
                    self._emit_node_focus(nodes)
            return

        if "[TOOL] 成功" in line:
            self._finalize_tool("成功")
            return
        if "[TOOL] 错误: " in line:
            msg = line.split("[TOOL] 错误: ", 1)[1][:200]
            self._finalize_tool(f"错误: {msg}")
            return

        if "[TEST] exit_code: " in line:
            code = line.split("exit_code: ", 1)[1].strip()
            self.on_event("tool", {
                "tool": "run_test", "args": {"command": self.test_command},
                "output": f"exit_code: {code}",
            })
            return
        if "[TEST] stdout: " in line:
            self.on_event("text", {"delta": "\n```\n" + line.split("[TEST] stdout: ", 1)[1][:300] + "\n```\n"})
            return
        if "[TEST] stderr: " in line:
            self.on_event("text", {"delta": "\n```\n" + line.split("[TEST] stderr: ", 1)[1][:300] + "\n```\n"})
            return

        if "[SUCCESS]" in line:
            tail = line.split("[SUCCESS] ", 1)[1] if "[SUCCESS] " in line else ""
            self._final_reply = f"✅ 修复成功！{tail}"
            m = re.search(r"共尝试 (\d+) 次", line)
            self.on_event("state", {"state": "success", "attempt": int(m.group(1)) if m else 0})
            self.on_event("text", {"delta": f"\n{line.strip()}\n"})
            return
        if "[FAIL]" in line:
            tail = line.split("[FAIL] ", 1)[1] if "[FAIL] " in line else ""
            self._final_reply = f"❌ 修复失败：{tail}"
            m = re.search(r"共尝试 (\d+) 次|用尽 (\d+) 次", line)
            self.on_event("state", {"state": "fail", "attempt": int(m.group(1) or m.group(2)) if m else 0})
            self.on_event("text", {"delta": f"\n{line.strip()}\n"})
            return

        for tag in _TEXT_TAGS:
            if tag in line:
                self.on_event("text", {"delta": line.strip()[:200] + "\n"})
                return

    # ---------- 主流程 ----------

    def run(self) -> dict:
        """同步运行修复任务，返回 done 事件负载（reply/iterations/latency_ms）。"""
        started = time.monotonic()
        self.on_event("turn_start", {
            "user_message": f"fix {self.bug_file} {self.test_command}",
        })

        # 初始上下文 burst：bug 文件函数 + 邻居先点亮
        ctx: list[str] = []
        if self.graph is not None:
            try:
                ctx = self.graph.context_nodes(
                    self.code_dir, os.path.basename(self.bug_file))
            except Exception:
                ctx = []
        self.on_event("graph_start", {"workflow": WORKFLOW, "nodes": ctx[:60]})
        for n in ctx[:60]:
            self.on_event("node_start", {"workflow": WORKFLOW, "node": n})
        self._hot = set(ctx[:60])

        cmd = ["python3", "-m", "swe_agent.main", "fix", self.bug_file,
               self.test_command, "--fsm"]
        proc = subprocess.Popen(
            cmd, cwd=self.project_root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        try:
            for raw in proc.stdout:
                self._handle_line(raw.rstrip("\n"))
        finally:
            proc.wait()
            if proc.stdout:
                for raw in proc.stdout:  # 吞掉残留输出，避免管道死锁
                    self._handle_line(raw.rstrip("\n"))

        # 收尾：熄灭所有高亮 + 结束事件
        for n in self._hot:
            self.on_event("node_end", {"workflow": WORKFLOW, "node": n})
        self._hot = set()
        latency_ms = int((time.monotonic() - started) * 1000)
        self.on_event("graph_end", {"workflow": WORKFLOW, "ms": latency_ms,
                                    "steps": self._iterations})
        if not self._final_reply:
            self._final_reply = "修复结束" + ("（成功）" if proc.returncode == 0 else "（失败）")
        self.on_event("turn_end", {"reply": self._final_reply,
                                   "iterations": self._iterations})
        done = {"reply": self._final_reply, "iterations": self._iterations,
                "latency_ms": latency_ms}
        self.on_event("done", done)
        return done
