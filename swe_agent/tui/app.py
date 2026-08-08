"""TUI 主应用：Claude Code 风格终端界面。"""

import asyncio
import json
import os
import time
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Footer, Input
from textual import work

from .models import Message, AppState
from .styles import CUSTOM_THEME, APP_CSS
from .widgets import StatusBar, MessageList, CommandPalette


class ChatApp(App):
    """TUI 主应用。"""

    CSS = APP_CSS

    BINDINGS = [
        Binding("escape", "quit", "退出"),
        Binding("ctrl+c", "cancel", "取消", show=False),
        Binding("ctrl+l", "clear", "清空"),
    ]

    def __init__(self, project_dir: Optional[str] = None):
        super().__init__()
        self.state = AppState()
        self.message_list: Optional[MessageList] = None
        self.status_bar: Optional[StatusBar] = None
        self.command_palette: Optional[CommandPalette] = None
        self.start_time = time.time()
        # 工作目录：--project 指定优先，否则用 cwd（生图只对这个目录向下扫描）
        self.current_dir = os.path.abspath(project_dir) if project_dir else os.getcwd()
        self.conversation_history: list[dict] = []
        self.thinking_collapsed: bool = True
        self._init_agent()

    def _init_agent(self) -> None:
        from swe_agent.llm.client import LLMClient
        from swe_agent.tools.schemas import TOOLS
        self.llm_client = LLMClient()
        self.tools = TOOLS
        self.tool_registry = None

    def _ensure_registry(self) -> None:
        if self.tool_registry is None:
            from swe_agent.tools.registry import ToolRegistry
            from swe_agent.graph import GraphManager
            # 首次对话触发建图：只对工作目录（self.current_dir）向下扫描，
            # 不向上扫上级目录（builder os.walk 以 code_dir 为根）
            graph_mgr = GraphManager(self.current_dir)
            graph_index = graph_mgr.build()
            summary = graph_index.get_summary()
            self._add_system(
                f"已为工作目录建图: {self.current_dir}\n"
                f"  节点 {summary['node_count']} / 边 {summary['edge_count']} / "
                f"文件 {summary['file_count']}（只含此目录及子目录）")
            skeleton_text = graph_index.generate_skeleton_text()
            self.tool_registry = ToolRegistry(
                skeleton_text=skeleton_text,
                code_dir=self.current_dir,
                graph_index=graph_index,
                graph_manager=graph_mgr,
            )

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar()
        yield MessageList()
        yield CommandPalette()
        yield Input(placeholder="输入消息或 / 查看命令", id="input-area")
        yield Footer()

    def on_mount(self) -> None:
        self.message_list = self.query_one(MessageList)
        self.status_bar = self.query_one(StatusBar)
        self.command_palette = self.query_one(CommandPalette)
        self._add_system("mini-swe v2.0 — 自动化代码修复 Agent")
        self._add_system(f"目录: {self.current_dir}")

    # === 消息 API ===

    def _add_system(self, content: str) -> None:
        msg = Message(content=content, msg_type="system")
        if self.message_list:
            self.message_list.add_message(msg)

    def add_system_message(self, content: str) -> None:
        self._add_system(content)

    def add_user_message(self, content: str) -> None:
        msg = Message(content=content, msg_type="user")
        if self.message_list:
            self.message_list.add_message(msg)

    def add_ai_message(self, content: str) -> None:
        msg = Message(content=content, msg_type="assistant")
        if self.message_list:
            self.message_list.add_message(msg)

    def add_tool_call(self, tool_name: str, arguments: dict,
                      success: bool = True, duration: float = 0.0) -> None:
        msg = Message(
            content=f"{tool_name}({json.dumps(arguments, ensure_ascii=False)})",
            msg_type="tool_call",
            tool_name=tool_name,
            tool_args=arguments,
            is_error=not success,
            duration=duration,
        )
        if self.message_list:
            self.message_list.add_message(msg)

    def add_tool_result(self, content: str, success: bool = True) -> None:
        msg = Message(content=content, msg_type="tool_result", is_error=not success)
        if self.message_list:
            self.message_list.add_message(msg)

    def add_thinking(self, content: str, collapsed: bool = True) -> None:
        msg = Message(content=content, msg_type="thinking", is_collapsed=collapsed)
        if self.message_list:
            self.message_list.add_message(msg)

    def add_code_diff(self, old: str, new: str) -> None:
        msg = Message(content=f"- {old}\n+ {new}", msg_type="diff")
        if self.message_list:
            self.message_list.add_message(msg)

    def set_status(self, status: str, step: int = 0, token_usage: int = 0) -> None:
        self.state.status = status
        self.state.step = step
        self.state.token_usage = token_usage
        self.state.elapsed_time = time.time() - self.start_time
        if self.status_bar:
            self.status_bar.update_state(self.state)
        try:
            input_widget = self.query_one(Input)
            if status in ["running", "thinking", "calling_tool"]:
                input_widget.disabled = True
                input_widget.placeholder = "处理中..."
            else:
                input_widget.disabled = False
                input_widget.placeholder = "输入消息或 / 查看命令"
        except Exception:
            pass

    def show_loading(self, text: str = "思考中...") -> None:
        if self.message_list:
            self.message_list.show_loading(text)

    def hide_loading(self) -> None:
        if self.message_list:
            self.message_list.hide_loading()

    # === 命令补全 ===

    def action_hide_palette(self) -> None:
        if self.command_palette:
            self.command_palette.hide()
        try:
            self.query_one(Input).focus()
        except Exception:
            pass

    def on_command_palette_selected(self, event: CommandPalette.Selected) -> None:
        """命令菜单选中。"""
        cmd = event.command
        if self.command_palette:
            self.command_palette.hide()
        # 清空输入框
        try:
            self.query_one(Input).value = ""
        except Exception:
            pass
        # 执行命令
        self._execute_command(cmd)

    def _execute_command(self, text: str) -> None:
        """执行命令（直接命令或自然语言）。"""
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        self.add_user_message(text)

        if cmd == "/help":
            self._show_help()
        elif cmd == "/status":
            self._show_status()
        elif cmd == "/clear":
            self.action_clear()
        elif cmd == "/reset":
            self.conversation_history.clear()
            self._add_system("对话历史已重置")
        elif cmd == "/quit":
            self.exit()
        elif cmd == "ls":
            self._handle_ls(arg)
        elif cmd == "cd":
            self._handle_cd(arg)
        elif cmd == "cat":
            self._handle_cat(arg)
        elif cmd == "fix":
            self._handle_fix(arg)
        else:
            # 自然语言 → LLM
            self.set_status("thinking")
            self._run_agent_chat(text)

    def _show_help(self) -> None:
        self._add_system(
            "命令:\n"
            "  ls [路径]   浏览目录\n"
            "  cd [路径]   切换目录\n"
            "  cat [文件]  查看文件\n"
            "  fix [文件]  修复 bug\n"
            "  /help       帮助\n"
            "  /status     状态\n"
            "  /clear      清空\n"
            "  /reset      重置对话\n"
            "  /quit       退出"
        )

    def _show_status(self) -> None:
        self._add_system(
            f"状态: {self.state.status}\n"
            f"步骤: {self.state.step}/{self.state.max_steps}\n"
            f"Token: {self.state.token_usage}\n"
            f"目录: {self.current_dir}\n"
            f"对话轮数: {len(self.conversation_history)}"
        )

    # === 直接命令处理（不走 LLM，瞬间响应）===

    def _handle_ls(self, path: str = "") -> None:
        target = os.path.join(self.current_dir, path) if path else self.current_dir
        target = os.path.expanduser(target)
        if not os.path.exists(target):
            self._add_system(f"不存在: {target}")
            return
        if os.path.isfile(target):
            self._add_system(f"文件: {target}")
            return
        try:
            entries = sorted(os.listdir(target))
            dirs, files = [], []
            for e in entries:
                if e.startswith("."):
                    continue
                full = os.path.join(target, e)
                if os.path.isdir(full):
                    dirs.append(f"  📁 {e}/")
                else:
                    files.append(f"     {e}")
            self._add_system("\n".join(dirs + files) or "(空目录)")
        except PermissionError:
            self._add_system(f"无权限: {target}")

    def _handle_cd(self, path: str = "") -> None:
        if not path:
            self.current_dir = os.path.expanduser("~")
        else:
            target = os.path.join(self.current_dir, path) if not os.path.isabs(path) else path
            target = os.path.expanduser(target)
            if os.path.isdir(target):
                self.current_dir = target
                self.tool_registry = None
            else:
                self._add_system(f"不存在: {target}")
                return
        self._add_system(f"目录: {self.current_dir}")

    def _handle_cat(self, filename: str) -> None:
        filepath = os.path.join(self.current_dir, filename) if not os.path.isabs(filename) else filename
        if not os.path.exists(filepath):
            self._add_system(f"不存在: {filepath}")
            return
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            self._add_system(f"{filepath}\n{content}")
        except Exception as e:
            self._add_system(f"读取失败: {e}")

    def _handle_fix(self, filepath: str) -> None:
        if not os.path.isabs(filepath):
            filepath = os.path.join(self.current_dir, filepath)
        if not os.path.exists(filepath):
            self._add_system(f"不存在: {filepath}")
            return
        test_file = self._find_test_file(filepath)
        test_cmd = f"pytest {test_file} -v" if test_file else f"pytest {filepath} -v"
        self.add_system_message(f"修复: {filepath}")
        self.add_system_message(f"测试: {test_cmd}")
        self._run_agent_fix(filepath, test_cmd)

    def _find_test_file(self, filepath: str) -> Optional[str]:
        dirname = os.path.dirname(filepath)
        basename = os.path.basename(filepath)
        clean_name = basename.replace("bug_", "", 1) if basename.startswith("bug_") else basename
        patterns = [f"test_{basename}", f"test_{clean_name}", f"{basename}_test.py", f"{clean_name}_test.py"]
        for pattern in patterns:
            test_path = os.path.join(dirname, pattern)
            if os.path.exists(test_path):
                return test_path
        tests_dir = os.path.join(dirname, "tests")
        if os.path.isdir(tests_dir):
            for pattern in patterns:
                test_path = os.path.join(tests_dir, pattern)
                if os.path.exists(test_path):
                    return test_path
        return None

    # === Agent 逻辑 ===

    @work(exclusive=True, group="agent")
    async def _run_agent_chat(self, user_message: str) -> None:
        self.set_status("thinking")
        try:
            self._ensure_registry()
            if not self.conversation_history:
                self.conversation_history.append({
                    "role": "system",
                    "content": self._build_system_prompt(),
                })
            self.conversation_history.append({"role": "user", "content": user_message})

            # 多轮工具循环（TUI 修复）：每轮流式思考 + 执行工具，直到无工具调用
            # 根因：旧实现只做"一轮工具 + 一次收尾"，收尾那轮若还想调工具会被静默丢弃 → 对话断流
            from .widgets.thinking_widget import ThinkingWidget
            loop = asyncio.get_event_loop()
            max_rounds = 8

            for _round in range(max_rounds):
                thinking_widget = ThinkingWidget("思考中...", collapsed=False)
                thinking_content = []
                thinking_count = [0]
                response_content = []

                def on_reasoning(token, _w=thinking_widget, _c=thinking_content, _n=thinking_count):
                    """思考链 token（在 executor 线程中运行）。每 5 个 token 刷新一次。"""
                    _c.append(token)
                    _n[0] += 1
                    if _n[0] % 5 == 0:
                        self.call_from_thread(_w.update_content, "".join(_c))

                def on_content(token, _c=response_content):
                    """内容 token（在 executor 线程中运行）。"""
                    _c.append(token)

                if self.message_list:
                    self.message_list.mount(thinking_widget)
                self.set_status("calling_tool")

                # LLM 调用（流式，在 executor 线程中，不阻塞 UI）
                response = await loop.run_in_executor(
                    None,
                    lambda: self.llm_client.chat(
                        messages=self.conversation_history,
                        tools=self.tools,
                        thinking=True,
                        reasoning_effort="max",
                        stream=True,
                        on_reasoning_token=on_reasoning,
                        on_token=on_content,
                    ),
                )

                # 最终更新思考链（保证短思考也能完整显示）
                if thinking_content:
                    thinking_widget.update_content("".join(thinking_content))
                    thinking_widget.collapsed = True

                final_content = "".join(response_content) if response_content else (response.content or "")

                # 记录 assistant 消息（含 tool_calls，供下一轮使用）
                assistant_msg = {"role": "assistant", "content": final_content}
                if response.reasoning_content:
                    assistant_msg["reasoning_content"] = response.reasoning_content
                if response.tool_calls:
                    assistant_msg["tool_calls"] = response.tool_calls
                self.conversation_history.append(assistant_msg)

                if not response.tool_calls:
                    # 无工具调用 → 最终回复
                    if final_content:
                        self.add_ai_message(final_content)
                    break

                # 有工具调用 → 逐个执行，然后进入下一轮（不再断流）
                for tc in response.tool_calls:
                    func_name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    self.add_tool_call(func_name, args)

                    result = await loop.run_in_executor(
                        None,
                        lambda n=func_name, a=args: self.tool_registry.execute(n, a),
                    )

                    result_data = json.loads(result)
                    if "error" in result_data:
                        self.add_tool_result(result_data["error"], False)
                    else:
                        self.add_tool_result(result[:300], True)

                    self.conversation_history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })
            else:
                # for 正常结束（未 break）= 达到 max_rounds 仍无最终回复
                self.add_ai_message("（已达到最大对话轮数，工具循环可能仍未收敛）")

            self.set_status("idle")
        except Exception as e:
            self.add_ai_message(f"错误: {e}")
            self.set_status("fail")

    @work(exclusive=True, group="agent")
    async def _run_agent_fix(self, filepath: str, test_cmd: str) -> None:
        self.set_status("running")
        try:
            cmd = ["python3", "-m", "swe_agent.main", "fix", filepath, test_cmd, "--fsm"]
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=project_root,
            )
            step_count = 0
            try:
                async with asyncio.timeout(120):
                    async for line in process.stdout:
                        decoded = line.decode("utf-8", errors="replace").strip()
                        if decoded:
                            self._parse_agent_output(decoded)
                            if "[TOOL]" in decoded and "调用" in decoded:
                                step_count += 1
                                self.set_status("running", step_count)
            except asyncio.TimeoutError:
                process.kill()
                self.add_ai_message("超时（2分钟）")
            stderr = await process.stderr.read()
            if stderr:
                self.add_system_message(f"错误: {stderr.decode('utf-8', errors='replace')[:500]}")
            await process.wait()
            if process.returncode == 0:
                self.set_status("success", step_count)
                self.add_ai_message("修复成功！")
            else:
                self.set_status("fail", step_count)
                self.add_ai_message("修复失败")
        except Exception as e:
            self.add_ai_message(f"错误: {e}")
            self.set_status("fail")

    _STATE_LABELS = {
        "init": "初始化", "locate": "定位", "patch": "补丁",
        "check": "检查", "test": "测试", "rollback": "回滚",
        "success": "成功", "fail": "失败",
    }

    def _parse_agent_output(self, line: str) -> None:
        if "[STATE]" in line:
            state = line.split("[STATE] ")[1].split(" (")[0]
            label = self._STATE_LABELS.get(state, state)
            self._add_system(f"▶ 状态流转 → {label}")
        elif "[INIT]" in line:
            self._add_system(f"INIT — {line.replace('[INIT] ', '')}")
        elif "[LOCATE]" in line:
            self._add_system("LOCATE — 分析代码...")
        elif "[PATCH]" in line:
            self._add_system("PATCH — 应用修复...")
        elif "[CHECK]" in line:
            self._add_system(f"CHECK — {line.replace('[CHECK] ', '')}")
        elif "[ROLLBACK]" in line:
            self._add_system(f"ROLLBACK — {line.replace('[ROLLBACK] ', '')}")
        elif "[MODE]" in line:
            self._add_system(f"MODE — {line.replace('[MODE] ', '')}")
        elif "[BUDGET]" in line:
            self._add_system(f"BUDGET — {line.replace('[BUDGET] ', '')}")
        elif "[SYNTAX]" in line:
            self._add_system(f"SYNTAX — {line.replace('[SYNTAX] ', '')}")
        elif "[LOG]" in line:
            self._add_system(f"LOG — {line.replace('[LOG] ', '')[:120]}")
        elif "[TEST]" in line and "运行测试" in line:
            self._add_system("TEST — 运行测试...")
        elif "[SUCCESS]" in line:
            self._add_system(f"SUCCESS — {line.replace('[SUCCESS] ', '')}")
        elif "[FAIL]" in line:
            self._add_system(f"FAIL — {line.replace('[FAIL] ', '')}")
        elif "[TOOL]" in line and "调用" in line:
            try:
                tool_part = line.split("调用 ")[1].split("(")[0]
                args = line.split("(")[1].split(")")[0] if "(" in line else ""
                self.add_tool_call(tool_part, {"args": args})
            except Exception:
                pass
        elif "[TOOL]" in line and "成功" in line:
            self.add_tool_result("成功", success=True)
        elif "[TOOL]" in line and "错误" in line:
            error = line.split("错误: ")[1] if "错误: " in line else "未知错误"
            self.add_tool_result(error, success=False)
        elif "[TEST]" in line and "exit_code:" in line:
            code = line.split("exit_code: ")[1]
            self.add_tool_result(f"退出码: {code}", success=(code == "0"))

    def _build_system_prompt(self) -> str:
        return f"""你是一个智能助手，可以帮用户浏览文件、查看代码、修复 bug。

当前目录: {self.current_dir}

可用工具:
- run_command(command): 运行终端命令
- search_function(name): 搜索函数
- view_file(file_path, function="函数名" | line+context | start_line+end_line): 查看代码
- edit_function(file_path, start_line, end_line, new_code): 编辑代码
- run_test(command): 运行测试

请根据用户需求自动使用合适的工具完成任务。"""

    # === 输入处理 ===

    def on_input_changed(self, event: Input.Changed) -> None:
        """输入变化：显示/隐藏命令菜单。"""
        text = event.value
        if text.startswith("/") and self.command_palette:
            self.command_palette.filter_commands(text)
            if not self.command_palette.is_visible():
                self.command_palette.show()
        elif self.command_palette and self.command_palette.is_visible():
            self.command_palette.hide()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if self.command_palette:
            self.command_palette.hide()
        self._execute_command(text)

    def action_clear(self) -> None:
        if self.message_list:
            self.message_list.clear_messages()
            self._add_system("已清空")

    def action_quit(self) -> None:
        """Escape 退出：先关闭命令菜单，再退出。"""
        # 如果命令菜单开着，先关菜单
        if self.command_palette and self.command_palette.is_visible():
            self.command_palette.hide()
            return
        # 取消所有正在运行的 worker
        for worker in self.workers:
            worker.cancel()
        self.exit()

    def action_cancel(self) -> None:
        """Ctrl+C：取消当前操作，不退出。"""
        for worker in self.workers:
            worker.cancel()
        self.set_status("idle")
        self._add_system("已取消")


def run_tui(project_dir: Optional[str] = None):
    """启动 TUI。project_dir 指定工作目录（生图范围），None 用 cwd。"""
    app = ChatApp(project_dir=project_dir)
    app.run()


if __name__ == "__main__":
    run_tui()
