"""TUI 主应用：基于 textual 的终端界面，集成 Agent 逻辑。"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional, Callable

from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal, ScrollableContainer
from textual.widgets import Header, Footer, Static, Input
from textual.binding import Binding
from textual import work

from .models import Message, AppState
from .styles import STATUS_COLORS


class StatusBar(Static):
    """状态栏组件。"""

    def __init__(self):
        super().__init__()
        self.state = AppState()

    def update_state(self, state: AppState) -> None:
        self.state = state
        self.refresh()

    def render(self) -> str:
        status = self.state.status.upper()
        color = STATUS_COLORS.get(self.state.status, "white")
        return (
            f"[{color}]{status}[/{color}]    "
            f"步骤: {self.state.step}/{self.state.max_steps}    "
            f"Token: {self.state.token_usage}    "
            f"耗时: {self.state.elapsed_time:.1f}s"
        )


class MessageWidget(Static):
    """单条消息组件。"""

    def __init__(self, msg: Message, on_toggle: Optional[Callable] = None):
        super().__init__()
        self.msg = msg
        self.on_toggle = on_toggle
        # 思考链允许自动扩展高度
        if msg.msg_type == "thinking":
            self.auto_height = True

    def on_click(self) -> None:
        """点击切换思考链。"""
        if self.msg.msg_type == "thinking" and self.on_toggle:
            self.on_toggle(self)

    def render(self) -> str:
        msg = self.msg
        if msg.msg_type == "user":
            return f"[bold cyan][You][/bold cyan] {msg.content}"
        elif msg.msg_type == "assistant":
            return f"[bold green][AI][/bold green] {msg.content}"
        elif msg.msg_type == "tool_call":
            if msg.is_error:
                return f"[bold red][ERR][/bold red] [red]{msg.tool_name}[/red]"
            else:
                duration = f"  {msg.duration:.1f}s" if msg.duration else ""
                return f"[bold green][OK][/bold green] [green]{msg.tool_name}[/green]{duration}"
        elif msg.msg_type == "tool_result":
            style = "green" if not msg.is_error else "red"
            return f"  [{style}]{msg.content}[/{style}]"
        elif msg.msg_type == "thinking":
            if msg.is_collapsed:
                return "[dim]  > 思考链 [+] (点击展开)[/dim]"
            else:
                # 确保内容显示完整
                content = msg.content if msg.content else "思考中..."
                return f"[dim]  > 思考链 [-] (点击缩起):\n{content}[/dim]"
        elif msg.msg_type == "diff":
            lines = msg.content.split("\n")
            result = []
            for line in lines:
                if line.startswith("-"):
                    result.append(f"[red]{line}[/red]")
                elif line.startswith("+"):
                    result.append(f"[green]{line}[/green]")
                else:
                    result.append(line)
            return "\n".join(result)
        elif msg.msg_type == "system":
            return f"[bold yellow][SYS][/bold yellow] [yellow]{msg.content}[/yellow]"
        elif msg.msg_type == "loading":
            return f"[dim]{msg.content}[/dim]"
        else:
            return msg.content


class MessageList(ScrollableContainer):
    """消息列表组件。"""

    def __init__(self):
        super().__init__()
        self.message_widgets: list[MessageWidget] = []
        self.loading_widget: Optional[MessageWidget] = None

    def add_message(self, msg: Message, on_toggle: Optional[Callable] = None) -> MessageWidget:
        widget = MessageWidget(msg, on_toggle=on_toggle)
        self.mount(widget)
        self.message_widgets.append(widget)
        # 延迟滚动，确保布局完成
        self.call_after_refresh(lambda: self.scroll_end(animate=False))
        return widget

    def show_loading(self, text: str = "思考中...") -> None:
        if self.loading_widget:
            self.loading_widget.remove()
        msg = Message(content=f"  {text}", msg_type="loading")
        self.loading_widget = self.add_message(msg)

    def hide_loading(self) -> None:
        if self.loading_widget:
            self.loading_widget.remove()
            self.loading_widget = None

    def clear_messages(self) -> None:
        self.remove_children()
        self.message_widgets.clear()
        self.loading_widget = None
        # 清理思考链引用
        if hasattr(self, '_thinking_widgets'):
            self._thinking_widgets.clear()


class ChatApp(App):
    """TUI 主应用。"""

    CSS = """
    Screen { layout: vertical; }
    #status-bar { height: 1; dock: top; background: $accent-darken-2; color: $text; padding: 0 1; }
    #message-list { height: 1fr; overflow-y: auto; padding: 0 1; }
    #input-field { height: 3; dock: bottom; border-top: solid $accent; padding: 0 1; }
    Input { background: $surface; color: $text; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出"),
        Binding("ctrl+l", "clear", "清空"),
    ]

    def __init__(self):
        super().__init__()
        self.state = AppState()
        self.message_list: Optional[MessageList] = None
        self.status_bar: Optional[StatusBar] = None
        self.start_time = time.time()
        self.current_dir = os.getcwd()

        # 对话历史（多轮）
        self.conversation_history: list[dict] = []

        # 思考链偏好（记住用户上次的选择）
        self.thinking_collapsed: bool = True

        # Agent 组件
        self._init_agent()

    def _init_agent(self) -> None:
        """初始化 Agent 组件。"""
        from swe_agent.llm.client import LLMClient
        from swe_agent.tools.registry import ToolRegistry
        from swe_agent.tools.schemas import TOOLS

        self.llm_client = LLMClient()
        self.tools = TOOLS
        self.tool_registry = None  # 延迟初始化

    def _ensure_registry(self) -> None:
        """确保 ToolRegistry 已初始化。"""
        if self.tool_registry is None:
            from swe_agent.tools.registry import ToolRegistry
            from swe_agent.ast_view.skeleton import SkeletonTree

            # 扫描当前目录
            tree = SkeletonTree(self.current_dir)
            tree.scan()
            skeleton_text = tree.generate_skeleton()

            self.tool_registry = ToolRegistry(
                skeleton_text=skeleton_text,
                code_dir=self.current_dir,
            )

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar()
        yield MessageList()
        yield Input(placeholder="输入消息或 /help 查看命令", id="input-field")
        yield Footer()

    def on_mount(self) -> None:
        self.message_list = self.query_one(MessageList)
        self.status_bar = self.query_one(StatusBar)

        self._add_system("mini-swe v2.0 - 自动化代码修复 Agent")
        self._add_system(f"当前目录: {self.current_dir}")
        self._add_system("输入文件路径修复 bug，或直接对话")

    def _add_system(self, content: str) -> None:
        msg = Message(content=content, msg_type="system")
        if self.message_list:
            self.message_list.add_message(msg)

    def add_system_message(self, content: str) -> None:
        """添加系统消息（公开接口）。"""
        self._add_system(content)

    async def _display_response(self, content: str) -> None:
        """显示 AI 响应，处理 DSML 格式的工具调用（异步）。"""
        import re
        import json
        import asyncio

        # 检查是否有 DSML 格式的工具调用
        if "tool_calls" in content and "DSML" in content:
            # 提取所有 invoke
            invokes = re.findall(
                r'<｜｜DSML｜｜invoke name="(\w+)">(.*?)</｜｜DSML｜｜invoke>',
                content,
                re.DOTALL
            )

            for tool_name, params_xml in invokes:
                # 提取参数
                params = {}
                param_matches = re.findall(
                    r'<｜｜DSML｜｜parameter name="(\w+)"[^>]*>(.*?)</｜｜DSML｜｜parameter>',
                    params_xml
                )
                for param_name, param_value in param_matches:
                    params[param_name] = param_value

                # 异步执行工具
                self.add_tool_call(tool_name, params)
                if self.tool_registry:
                    # 在线程池中执行工具，避免阻塞 UI
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.tool_registry.execute(tool_name, params)
                    )
                    result_data = json.loads(result)
                    if "error" in result_data:
                        self.add_tool_result(result_data["error"], success=False)
                    else:
                        display = result[:300] + "..." if len(result) > 300 else result
                        self.add_tool_result(display, success=True)

            # 显示 AI 消息（去掉工具调用部分）
            clean_content = re.sub(
                r'<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>',
                '',
                content,
                flags=re.DOTALL
            ).strip()
            if clean_content:
                self.add_ai_message(clean_content)
        else:
            # 普通内容
            self.add_ai_message(content)

    def _update_status(self) -> None:
        self.state.elapsed_time = time.time() - self.start_time
        if self.status_bar:
            self.status_bar.update_state(self.state)

    # === 公开 API ===

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
        args_str = json.dumps(arguments, ensure_ascii=False)
        msg = Message(
            content=f"{tool_name}({args_str})",
            msg_type="tool_call",
            tool_name=f"{tool_name}({args_str})",
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
            widget = self.message_list.add_message(msg, on_toggle=self._toggle_thinking)
            # 保存引用以便后续切换
            if not hasattr(self, '_thinking_widgets'):
                self._thinking_widgets = []
            self._thinking_widgets.append(widget)

    def _toggle_thinking(self, widget: MessageWidget) -> None:
        """切换思考链展开/缩起。"""
        widget.msg.is_collapsed = not widget.msg.is_collapsed
        self.thinking_collapsed = widget.msg.is_collapsed
        # 只刷新布局，不滚动
        widget.refresh(layout=True)

    def set_status(self, status: str, step: int = 0, token_usage: int = 0) -> None:
        self.state.status = status
        self.state.step = step
        self.state.token_usage = token_usage
        self._update_status()

        # 禁用/启用输入框
        try:
            input_widget = self.query_one(Input)
            if status in ["running", "thinking", "calling_tool"]:
                input_widget.disabled = True
                input_widget.placeholder = "... 正在处理中，请等待 ..."
            else:
                input_widget.disabled = False
                input_widget.placeholder = "输入消息或 /help 查看命令"
        except:
            pass

    def show_loading(self, text: str = "思考中...") -> None:
        if self.message_list:
            self.message_list.show_loading(text)

    def hide_loading(self) -> None:
        if self.message_list:
            self.message_list.hide_loading()

    def _update_status_bar(self) -> None:
        """更新状态栏显示运行状态。"""
        if self.status_bar:
            self.status_bar.refresh()

    # === Agent 核心逻辑 ===

    @work(exclusive=True, group="agent")
    async def _run_agent_chat(self, user_message: str) -> None:
        """运行 Agent 对话（异步，支持多轮对话和思考链）。"""
        import json
        import time as time_module

        # 立即更新状态
        self.set_status("thinking")

        try:
            self._ensure_registry()

            # 如果是第一轮，添加系统提示
            if not self.conversation_history:
                system_prompt = self._build_system_prompt()
                self.conversation_history.append({"role": "system", "content": system_prompt})

            # 添加用户消息到历史
            self.conversation_history.append({"role": "user", "content": user_message})

            # 工具执行函数
            def tool_executor(tool_name: str, arguments: dict) -> str:
                start = time_module.time()
                self.add_tool_call(tool_name, arguments)

                result = self.tool_registry.execute(tool_name, arguments)
                duration = time_module.time() - start

                result_data = json.loads(result)
                if "error" in result_data:
                    self.add_tool_result(result_data["error"], success=False)
                else:
                    display = result[:200] + "..." if len(result) > 200 else result
                    self.add_tool_result(display, success=True)

                return result

            # 调用 LLM（带思考模式，流式输出）
            self.set_status("calling_tool")

            # 创建思考链 widget（用于流式更新）
            thinking_msg = Message(content="思考中...", msg_type="thinking", is_collapsed=self.thinking_collapsed)
            thinking_widget = None
            if self.message_list:
                thinking_widget = self.message_list.add_message(thinking_msg, on_toggle=self._toggle_thinking)
                if not hasattr(self, '_thinking_widgets'):
                    self._thinking_widgets = []
                self._thinking_widgets.append(thinking_widget)

            # 流式输出
            import asyncio
            thinking_content = []
            token_queue = asyncio.Queue()
            token_count = [0]

            def on_token(token: str):
                thinking_content.append(token)
                token_count[0] += 1
                # 线程安全：放入队列，由主线程处理 UI 更新
                try:
                    token_queue.put_nowait(token)
                except:
                    pass

            # 在后台线程运行流式调用
            import concurrent.futures
            loop = asyncio.get_event_loop()

            async def run_streaming():
                """异步运行流式调用并更新 UI。"""
                future = loop.run_in_executor(
                    None,
                    lambda: self.llm_client.chat(
                        messages=self.conversation_history,
                        tools=self.tools,
                        thinking=True,
                        reasoning_effort="max",
                        stream=True,
                        on_token=on_token,
                    )
                )

                # 异步等待队列中的 token 并更新 UI
                while not future.done():
                    try:
                        # 等待新 token，超时 0.1 秒
                        token = await asyncio.wait_for(token_queue.get(), timeout=0.1)
                        # 在主线程中更新 UI
                        if thinking_widget:
                            thinking_msg.content = "".join(thinking_content)
                            thinking_widget.refresh(layout=True)
                            if self.message_list:
                                self.message_list.scroll_end(animate=False)
                    except asyncio.TimeoutError:
                        # 没有新 token，让出控制权
                        await asyncio.sleep(0.01)
                        continue

                # 处理剩余的 token
                while not token_queue.empty():
                    try:
                        token_queue.get_nowait()
                        if thinking_widget:
                            thinking_msg.content = "".join(thinking_content)
                            thinking_widget.refresh(layout=True)
                    except:
                        break

                return future.result()

            response = await run_streaming()

            # 调试信息
            debug_info = f"流式 tokens: {token_count[0]}, reasoning: {bool(response.reasoning_content)}, content: {bool(response.content)}"
            self.add_tool_result(debug_info, success=True)

            # 更新思考链最终内容
            if thinking_widget:
                # 优先使用流式收集的内容，如果没有则用 API 返回的
                final_content = "".join(thinking_content) if thinking_content else response.reasoning_content
                if final_content:
                    thinking_msg.content = final_content
                    thinking_widget.refresh()
                else:
                    # 如果都没有内容，隐藏思考链
                    thinking_widget.remove()

            # 保存 assistant 消息到历史（包含 reasoning_content）
            assistant_msg = {"role": "assistant", "content": response.content or ""}
            if response.reasoning_content:
                assistant_msg["reasoning_content"] = response.reasoning_content
            if response.tool_calls:
                assistant_msg["tool_calls"] = response.tool_calls
            self.conversation_history.append(assistant_msg)

            # 处理工具调用
            if response.tool_calls:
                for tc in response.tool_calls:
                    func_name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    result = tool_executor(func_name, args)

                    # 保存工具结果到历史
                    self.conversation_history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })

                # 再次调用 LLM 获取最终回复
                self.set_status("calling_tool")
                final_response = self.llm_client.chat(
                    messages=self.conversation_history,
                    thinking=True,
                    reasoning_effort="max",
                )

                # 显示最终回复的思考链
                if final_response.reasoning_content:
                    self.add_thinking(final_response.reasoning_content, collapsed=True)

                # 保存最终回复到历史
                final_msg = {"role": "assistant", "content": final_response.content or ""}
                if final_response.reasoning_content:
                    final_msg["reasoning_content"] = final_response.reasoning_content
                self.conversation_history.append(final_msg)

                # 显示最终回复
                if final_response.content:
                    await self._display_response(final_response.content)
            else:
                # 没有工具调用，直接显示回复
                if response.content:
                    await self._display_response(response.content)

            self.set_status("idle")

        except Exception as e:
            self.add_ai_message(f"错误: {e}")
            self.set_status("fail")

    @work(exclusive=True, group="agent")
    async def _run_agent_fix(self, filepath: str, test_cmd: str) -> None:
        """运行 Agent 修复 bug（异步，显示详细状态）。"""
        import subprocess

        self.set_status("running")

        try:
            cmd = [
                "python3", "-m", "swe_agent.main", "fix",
                filepath, test_cmd, "--fsm"
            ]

            self.add_system_message(f"执行命令: {' '.join(cmd)}")

            # 使用项目根目录作为工作目录（不是文件所在目录）
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self.add_system_message(f"工作目录: {project_root}")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=project_root,
            )

            # 读取输出并解析（带超时）
            step_count = 0
            try:
                async with asyncio.timeout(120):  # 2 分钟超时
                    async for line in process.stdout:
                        decoded = line.decode('utf-8', errors='replace').strip()
                        if decoded:
                            # 解析并显示详细状态
                            self._parse_agent_output_detailed(decoded)
                            # 计算步骤
                            if "[TOOL]" in decoded and "调用" in decoded:
                                step_count += 1
                                self.set_status("running", step=step_count)
            except asyncio.TimeoutError:
                process.kill()
                self.add_ai_message("修复超时（2分钟）")

            # 读取 stderr
            stderr = await process.stderr.read()
            if stderr:
                self.add_system_message(f"错误输出: {stderr.decode('utf-8', errors='replace')[:500]}")

            await process.wait()
            self.add_system_message(f"进程退出码: {process.returncode}")

            self.add_system_message("─" * 40)

            if process.returncode == 0:
                self.set_status("success", step=step_count)
                self.add_ai_message("修复成功！")
            else:
                self.set_status("fail", step=step_count)
                self.add_ai_message("修复失败")

        except Exception as e:
            self.add_ai_message(f"错误: {e}")
            self.set_status("fail")

    def _parse_agent_output_detailed(self, line: str) -> None:
        """解析 Agent 输出并显示详细状态。"""
        # FSM 状态流转
        if "[INIT]" in line:
            self._add_system(f"[FSM] INIT - {line.replace('[INIT] ', '')}")
        elif "[LOCATE]" in line or "LOCATE" in line and "状态" in line:
            self._add_system(f"[FSM] LOCATE - 分析代码...")
        elif "[PATCH]" in line or "PATCH" in line and "状态" in line:
            self._add_system(f"[FSM] PATCH - 应用修复...")
        elif "[TEST]" in line and "运行测试" in line:
            self._add_system(f"[FSM] TEST - 运行测试...")
        elif "[SUCCESS]" in line:
            self._add_system(f"[FSM] SUCCESS - {line.replace('[SUCCESS] ', '')}")
        elif "[FAIL]" in line:
            self._add_system(f"[FSM] FAIL - {line.replace('[FAIL] ', '')}")

        # 工具调用
        elif "[TOOL]" in line and "调用" in line:
            try:
                tool_part = line.split("调用 ")[1].split("(")[0]
                args = line.split("(")[1].split(")")[0] if "(" in line else ""
                self.add_tool_call(tool_part, {"args": args})
            except:
                self.add_tool_call("unknown", {})

        # 工具结果
        elif "[TOOL]" in line and "成功" in line:
            self.add_tool_result("成功", success=True)
        elif "[TOOL]" in line and "错误" in line:
            error = line.split("错误: ")[1] if "错误: " in line else "未知错误"
            self.add_tool_result(error, success=False)

        # 测试结果
        elif "[TEST]" in line and "exit_code:" in line:
            code = line.split("exit_code: ")[1]
            self.add_tool_result(f"测试退出码: {code}", success=code == "0")
        elif "[TEST]" in line and "PASSED" in line:
            self.add_tool_result(line, success=True)
        elif "[TEST]" in line and "FAILED" in line:
            self.add_tool_result(line, success=False)

        # AST 压缩
        elif "骨架" in line or "skeleton" in line.lower():
            self._add_system(f"[AST] {line}")

        # 看门狗
        elif "WATCHDOG" in line or "watchdog" in line.lower():
            self._add_system(f"[看门狗] {line}")

        # 其他重要信息
        elif "[INIT]" in line or "[PATCH]" in line or "[TEST]" in line:
            # 已经处理过，跳过
            pass

    def _build_system_prompt(self) -> str:
        """构建系统提示。"""
        return f"""你是一个智能助手，可以帮助用户浏览文件、查看代码、修复 bug。

当前工作目录: {self.current_dir}

你可以使用以下工具:
- run_command(command): 运行终端命令（如 ls, cat, pwd, find 等）
- search_function(name): 搜索函数
- expand_function(file_path, func_name): 查看函数源码
- edit_function(file_path, start_line, end_line, new_code): 编辑代码
- run_test(command): 运行测试

当用户想浏览目录时，使用 run_command("ls") 或 run_command("ls -la")。
当用户想查看文件时，使用 run_command("cat 文件名")。
当用户想修复 bug 时，使用 run_test 运行测试。

请根据用户的需求自动使用合适的工具完成任务。"""

    def _parse_agent_output(self, line: str) -> None:
        """解析 Agent 输出。"""
        if "[TOOL]" in line and "调用" in line:
            try:
                tool_part = line.split("调用 ")[1].split("(")[0]
                self.add_tool_call(tool_part, {})
            except:
                pass
        elif "[TOOL]" in line and "成功" in line:
            self.add_tool_result("成功", success=True)
        elif "[TOOL]" in line and "错误" in line:
            error = line.split("错误: ")[1] if "错误: " in line else "未知错误"
            self.add_tool_result(error, success=False)
        elif "[TEST]" in line and "exit_code:" in line:
            code = line.split("exit_code: ")[1]
            self.add_tool_result(f"测试退出码: {code}", success=code == "0")
        elif "[SUCCESS]" in line:
            self.add_ai_message(line.replace("[SUCCESS] ", ""))
        elif "[FAIL]" in line:
            self.add_ai_message(line.replace("[FAIL] ", ""))

    # === 命令处理 ===

    def _handle_command(self, command: str) -> None:
        """处理斜杠命令。"""
        cmd = command.lower().strip()

        if cmd == "/help":
            self._add_system(
                "命令列表:\n"
                "  ls [路径]    - 浏览目录\n"
                "  cd [路径]    - 切换目录\n"
                "  cat [文件]   - 查看文件\n"
                "  fix [文件]   - 修复 bug\n"
                "  /help        - 显示帮助\n"
                "  /status      - 显示状态\n"
                "  /thinking    - 切换思考链展开/缩起\n"
                "  /clear       - 清空消息\n"
                "  /reset       - 重置对话历史\n"
                "  /quit        - 退出\n\n"
                "也可以直接对话，Agent 会自动使用工具"
            )
        elif cmd == "/status":
            self._add_system(
                f"状态: {self.state.status}\n"
                f"步骤: {self.state.step}/{self.state.max_steps}\n"
                f"Token: {self.state.token_usage}\n"
                f"目录: {self.current_dir}\n"
                f"对话轮数: {len(self.conversation_history)}"
            )
        elif cmd == "/clear":
            self.action_clear()
        elif cmd == "/reset":
            self.conversation_history.clear()
            self._add_system("对话历史已重置")
        elif cmd == "/thinking":
            self.thinking_collapsed = not self.thinking_collapsed
            status = "缩起" if self.thinking_collapsed else "展开"
            self._add_system(f"思考链默认状态: {status}")
        elif cmd == "/quit":
            self.exit()
        else:
            self._add_system(f"未知命令: {command}")

    def _handle_ls(self, path: str = "") -> None:
        """处理 ls 命令。"""
        target = os.path.join(self.current_dir, path) if path else self.current_dir
        target = os.path.expanduser(target)

        if not os.path.exists(target):
            self._add_system(f"路径不存在: {target}")
            return

        if os.path.isfile(target):
            self._add_system(f"这是一个文件: {target}")
            return

        try:
            entries = os.listdir(target)
            entries.sort()

            dirs = []
            files = []
            for e in entries:
                if e.startswith('.'):
                    continue
                full = os.path.join(target, e)
                if os.path.isdir(full):
                    dirs.append(f"  [dir]  {e}/")
                elif e.endswith('.py'):
                    files.append(f"  [file] {e}")
                else:
                    files.append(f"         {e}")

            output = f"目录: {target}\n"
            if dirs:
                output += "\n".join(dirs) + "\n"
            if files:
                output += "\n".join(files)

            self._add_system(output)
        except PermissionError:
            self._add_system(f"没有权限访问: {target}")

    def _handle_cd(self, path: str = "") -> None:
        """处理 cd 命令。"""
        if not path:
            self.current_dir = os.path.expanduser("~")
        else:
            target = os.path.join(self.current_dir, path) if not os.path.isabs(path) else path
            target = os.path.expanduser(target)
            if os.path.isdir(target):
                self.current_dir = target
                self.tool_registry = None  # 重置 registry
            else:
                self._add_system(f"目录不存在: {target}")
                return

        self._add_system(f"切换到: {self.current_dir}")

    def _handle_cat(self, filename: str) -> None:
        """处理 cat 命令。"""
        filepath = os.path.join(self.current_dir, filename) if not os.path.isabs(filename) else filename

        if not os.path.exists(filepath):
            self._add_system(f"文件不存在: {filepath}")
            return

        if not os.path.isfile(filepath):
            self._add_system(f"这不是文件: {filepath}")
            return

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            self._add_system(f"文件: {filepath}\n{content}")
        except Exception as e:
            self._add_system(f"读取失败: {e}")

    def _handle_fix(self, filepath: str) -> None:
        """处理修复 bug 命令。"""
        if not os.path.isabs(filepath):
            filepath = os.path.join(self.current_dir, filepath)

        if not os.path.exists(filepath):
            self._add_system(f"文件不存在: {filepath}")
            return

        if not os.path.isfile(filepath):
            self._add_system(f"这不是文件: {filepath}")
            return

        # 自动查找测试文件
        test_file = self._find_test_file(filepath)
        if test_file:
            test_cmd = f"pytest {test_file} -v"
        else:
            test_cmd = f"pytest {filepath} -v"

        # 显示修复开始信息
        self.add_system_message(f"开始修复: {filepath}")
        self.add_system_message(f"测试命令: {test_cmd}")
        self.add_system_message("─" * 40)

        self._run_agent_fix(filepath, test_cmd)

    def _find_test_file(self, filepath: str) -> Optional[str]:
        """查找对应的测试文件（递归搜索）。"""
        dirname = os.path.dirname(filepath)
        basename = os.path.basename(filepath)
        # 去掉 bug_ 前缀
        clean_name = basename.replace("bug_", "", 1) if basename.startswith("bug_") else basename

        # 1. 先在同目录查找
        patterns = [
            f"test_{basename}",           # test_bug_xxx.py
            f"test_{clean_name}",         # test_xxx.py
            f"{basename}_test.py",        # bug_xxx_test.py
            f"{clean_name}_test.py",      # xxx_test.py
        ]
        for pattern in patterns:
            test_path = os.path.join(dirname, pattern)
            if os.path.exists(test_path):
                return test_path

        # 2. 在 tests/ 子目录查找
        tests_dir = os.path.join(dirname, "tests")
        if os.path.isdir(tests_dir):
            for pattern in patterns:
                test_path = os.path.join(tests_dir, pattern)
                if os.path.exists(test_path):
                    return test_path

        # 3. 在父目录的 tests/ 查找
        parent_tests = os.path.join(os.path.dirname(dirname), "tests")
        if os.path.isdir(parent_tests):
            for pattern in patterns:
                test_path = os.path.join(parent_tests, pattern)
                if os.path.exists(test_path):
                    return test_path

        return None

    # === 事件处理 ===

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        # 立即清空输入框（无延迟）
        event.input.value = ""

        # 立即显示用户消息（无延迟）
        self.add_user_message(text)

        # 立即更新状态（无延迟）
        self.set_status("thinking")

        # 解析命令
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd.startswith("/"):
            self._handle_command(text)
        elif cmd == "ls":
            self._handle_ls(arg)
        elif cmd == "cd":
            self._handle_cd(arg)
        elif cmd == "cat":
            self._handle_cat(arg)
        elif cmd == "fix":
            self._handle_fix(arg)
        elif os.path.exists(text) or os.path.exists(os.path.join(self.current_dir, text)):
            self._handle_fix(text)
        else:
            # 异步调用 Agent（不阻塞 UI）
            self._run_agent_chat(text)

    def action_clear(self) -> None:
        if self.message_list:
            self.message_list.clear_messages()
            self._add_system("消息已清空")


def run_tui():
    """启动 TUI 应用。"""
    app = ChatApp()
    app.run()


if __name__ == "__main__":
    run_tui()
